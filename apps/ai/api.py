"""
El endpoint que el front consulta una vez para saber si la IA existe en esta
instalación y cuánto le queda al usuario este mes.

Es a propósito el único endpoint de IA por ahora: los que hacen trabajo
(recibos, parseo, chat) llegan con cada función, sobre `services.run()`. Éste
es el que permite que la app no muestre entradas de IA cuando no hay key, en
vez de mostrarlas y fallar al tocarlas.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import Wallet
from apps.common.api import HasWorkspaceMembership
from apps.transactions.services import RECEIPT_CONTENT_TYPES, RECEIPT_MAX_SIZE

from . import receipts, services
from .client import AIUnavailable


class QuotaSerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        allow_null=True, help_text="Tope del mes. null = sin tope."
    )
    used = serializers.IntegerField()
    remaining = serializers.IntegerField(allow_null=True)


class AIStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        help_text="False = esta instalación no tiene GEMINI_API_KEY; el cliente "
                  "debería ocultar todo lo de IA."
    )
    quotas = serializers.DictField(child=QuotaSerializer())
    resets_at = serializers.DateTimeField(
        help_text="Cuándo vuelven a cero los contadores (primer día del mes que viene)."
    )


@extend_schema(tags=["ai"], responses=AIStatusSerializer)
class AIStatusView(APIView):
    """Si la IA está disponible y cuánta cuota le queda al usuario.

    No pide `X-Workspace-ID`: la cuota es por usuario, no por presupuesto —
    quien paga es el dueño del plan, y la misma cuota se gasta desde
    cualquiera de sus workspaces.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(AIStatusSerializer(services.availability_for(request.user)).data)


# ---------------------------------------------------------------------------
# Escaneo de recibos
# ---------------------------------------------------------------------------
class ReceiptItemSerializer(serializers.Serializer):
    description = serializers.CharField()
    quantity = serializers.CharField(allow_null=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)


class PossibleDuplicateSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    description = serializers.CharField()


class ReceiptCandidateSerializer(serializers.Serializer):
    """Una **candidata**, no una transacción: todos los campos son editables en
    la app y ninguno se guarda hasta que el usuario confirma."""

    amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    tax_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField(
        allow_null=True,
        help_text="Lo que dice el recibo. La moneda real de la transacción sale de la cartera.",
    )
    date = serializers.DateField()
    merchant = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    category = serializers.UUIDField(allow_null=True)
    category_source = serializers.ChoiceField(
        choices=["history", "ai"], allow_null=True,
        help_text="De dónde salió la categoría: `history` = cómo categorizaste ese comercio "
                  "antes (se intenta primero, es gratis y determinista), `ai` = sugerencia del "
                  "modelo, `null` = no se pudo resolver.",
    )
    items = ReceiptItemSerializer(many=True)
    confidence = serializers.DictField(
        child=serializers.ChoiceField(choices=receipts.CONFIDENCE_LEVELS),
        help_text="Por campo (`amount`, `date`, `merchant`). `low` = marcalo para que el "
                  "usuario lo mire antes de guardar.",
    )
    possible_duplicates = PossibleDuplicateSerializer(many=True)


class ReceiptScanRequestSerializer(serializers.Serializer):
    """Sólo para el esquema: el parseo real lo hace la vista, que necesita el
    archivo crudo."""

    file = serializers.FileField(help_text="JPG, PNG, WEBP, HEIC o PDF, hasta 8 MB.")
    wallet = serializers.UUIDField(
        required=False,
        help_text="Opcional. Si viene, la respuesta trae los posibles duplicados de esa cartera.",
    )


class AIServiceUnavailable(APIException):
    """503 y no 500: que Gemini no conteste no es un error nuestro, y el
    cliente tiene que poder distinguir "volvé a intentar o cargalo a mano" de
    "algo se rompió"."""

    status_code = 503
    default_code = "ai_unavailable"
    default_detail = "No se pudo leer el recibo ahora mismo. Probá de nuevo o cargalo a mano."


@extend_schema(
    tags=["ai"],
    request=ReceiptScanRequestSerializer,
    responses={200: ReceiptCandidateSerializer},
)
class ReceiptScanView(APIView):
    """Lee un recibo y devuelve una candidata editable. **No crea nada.**

    El archivo tampoco se guarda acá: se sube como `Transaction.receipt` por
    `/transactions/{id}/receipt/` recién cuando el usuario confirma. Así un
    escaneo que el usuario descarta no deja basura en el bucket.
    """

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"file": "Requerido."})
        if file.size > RECEIPT_MAX_SIZE:
            raise ValidationError({"file": "El archivo pesa más de 8 MB."})
        if file.content_type not in RECEIPT_CONTENT_TYPES:
            raise ValidationError({"file": "Formato no soportado (usá JPG, PNG, WEBP, HEIC o PDF)."})

        try:
            candidate = receipts.scan(
                user=request.user,
                workspace=request.workspace,
                file_bytes=file.read(),
                content_type=file.content_type,
                wallet=self._wallet(request),
            )
        except AIUnavailable:
            raise AIServiceUnavailable()

        return Response(ReceiptCandidateSerializer(candidate).data)

    def _wallet(self, request):
        """La cartera contra la que buscar duplicados. Opcional, y se valida
        que sea del workspace del header: sin eso, un UUID ajeno dejaría ver
        montos y fechas de transacciones de otro presupuesto."""
        wallet_id = request.data.get("wallet")
        if not wallet_id:
            return None
        try:
            wallet = Wallet.objects.filter(
                id=wallet_id, workspace=request.workspace
            ).filter(
                Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=request.user)
            ).first()
        except (DjangoValidationError, ValueError):
            # Un UUID mal formado llega como texto cualquiera desde un
            # multipart: es un 400, no un 500.
            raise ValidationError({"wallet": "No es un UUID válido."})
        if wallet is None:
            raise ValidationError({"wallet": "No existe en este workspace."})
        return wallet
