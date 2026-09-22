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
from apps.common.api import AtomicOnlyForWritesMixin, HasWorkspaceMembership
from apps.transactions.services import RECEIPT_CONTENT_TYPES, RECEIPT_MAX_SIZE

from . import chat, parsing, receipts, services
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
class AIStatusView(AtomicOnlyForWritesMixin, APIView):
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


def _wallet_from(request):
    """La cartera contra la que buscar duplicados. Opcional en los dos
    endpoints, y siempre validada contra el workspace del header: sin eso, un
    UUID ajeno dejaría ver montos y fechas de otro presupuesto. Las privadas de
    las que el usuario no es dueño tampoco cuentan.
    """
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
        # Un UUID mal formado llega como texto cualquiera desde un multipart:
        # es un 400, no un 500.
        raise ValidationError({"wallet": "No es un UUID válido."})
    if wallet is None:
        raise ValidationError({"wallet": "No existe en este workspace."})
    return wallet


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
                wallet=_wallet_from(request),
            )
        except AIUnavailable:
            raise AIServiceUnavailable()

        return Response(ReceiptCandidateSerializer(candidate).data)



# ---------------------------------------------------------------------------
# Entrada por texto libre
# ---------------------------------------------------------------------------
class ParseCandidateSerializer(serializers.Serializer):
    """Lo mismo que el escaneo de recibos, más el tipo y la cartera: una frase
    puede nombrar las dos cosas ("me pagaron 800 a la cuenta") y un recibo no."""

    type = serializers.ChoiceField(choices=["expense", "income"])
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField(allow_null=True)
    date = serializers.DateField()
    merchant = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    wallet = serializers.UUIDField(allow_null=True)
    wallet_source = serializers.ChoiceField(
        choices=["text"], allow_null=True,
        help_text="`text` = la frase nombró una cartera; `null` = no, el cliente deja la suya.",
    )
    category = serializers.UUIDField(allow_null=True)
    category_source = serializers.ChoiceField(
        choices=["history", "ai"], allow_null=True,
        help_text="Igual que en `/ai/receipt/`: el historial se intenta primero.",
    )
    confidence = serializers.DictField(
        child=serializers.ChoiceField(choices=receipts.CONFIDENCE_LEVELS)
    )
    possible_duplicates = PossibleDuplicateSerializer(many=True)


class ParseRequestSerializer(serializers.Serializer):
    text = serializers.CharField(
        max_length=parsing.MAX_TEXT_LENGTH,
        help_text='La frase tal cual: "gasté 12.50 en almuerzo con la tarjeta".',
    )
    wallet = serializers.UUIDField(
        required=False,
        help_text="La cartera que el usuario ya tiene elegida, para buscar duplicados "
                  "cuando la frase no nombra ninguna.",
    )


@extend_schema(tags=["ai"], request=ParseRequestSerializer, responses={200: ParseCandidateSerializer})
class ParseTextView(APIView):
    """Convierte una frase suelta en una candidata editable. **No crea nada.**

    Es el mismo contrato que `/ai/receipt/`, y a propósito: el cliente muestra
    las dos con la misma pantalla, y los canales que vienen después (Telegram,
    voz) van a entrar por acá sin inventar un formato nuevo.
    """

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        text = (request.data.get("text") or "").strip()
        if not text:
            raise ValidationError({"text": "Requerido."})
        if len(text) > parsing.MAX_TEXT_LENGTH:
            raise ValidationError(
                {"text": f"Máximo {parsing.MAX_TEXT_LENGTH} caracteres."}
            )

        try:
            candidate = parsing.parse(
                user=request.user,
                workspace=request.workspace,
                text=text,
                wallet=_wallet_from(request),
            )
        except AIUnavailable:
            raise AIServiceUnavailable(
                "No se pudo leer la frase ahora mismo. Probá de nuevo o cargala a mano."
            )

        return Response(ParseCandidateSerializer(candidate).data)


# ---------------------------------------------------------------------------
# Voz / dictado
# ---------------------------------------------------------------------------
class VoiceRequestSerializer(serializers.Serializer):
    """Sólo para el esquema: el parseo real lo hace la vista, que necesita el
    archivo crudo."""

    file = serializers.FileField(help_text="WAV, MP3, AAC, OGG o FLAC, hasta 15 MB.")
    wallet = serializers.UUIDField(
        required=False,
        help_text="Opcional. Si viene, la respuesta trae los posibles duplicados de esa cartera.",
    )


@extend_schema(tags=["ai"], request=VoiceRequestSerializer, responses={200: ParseCandidateSerializer})
class VoiceParseView(APIView):
    """Convierte un dictado en una candidata editable. **No crea nada.**

    Mismo contrato que `/ai/parse/`: el audio se transcribe y se interpreta
    en una sola llamada a Gemini (sin un proveedor de transcripción aparte),
    y sale por el mismo parser -- ver `parsing.parse_audio`.
    """

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"file": "Requerido."})
        if file.size > parsing.AUDIO_MAX_SIZE:
            raise ValidationError({"file": "El audio pesa más de 15 MB."})
        if file.content_type not in parsing.AUDIO_CONTENT_TYPES:
            raise ValidationError({"file": "Formato no soportado (usá WAV, MP3, AAC, OGG o FLAC)."})

        try:
            candidate = parsing.parse_audio(
                user=request.user,
                workspace=request.workspace,
                audio_bytes=file.read(),
                content_type=file.content_type,
                wallet=_wallet_from(request),
            )
        except AIUnavailable:
            raise AIServiceUnavailable(
                "No se pudo procesar el audio ahora mismo. Probá de nuevo o escribilo a mano."
            )

        return Response(ParseCandidateSerializer(candidate).data)


# ---------------------------------------------------------------------------
# Chat sobre las finanzas del workspace
# ---------------------------------------------------------------------------
class ChatRequestSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=chat.MAX_QUESTION_LENGTH)


class ChatResponseSerializer(serializers.Serializer):
    answer = serializers.CharField()
    # Nombre de la función que se llamó para responder (ver `chat._FUNCTIONS`),
    # o `null` si la pregunta no daba para llamar ninguna. Informativo: el
    # cliente no necesita validarlo contra una lista cerrada.
    function_used = serializers.CharField(allow_null=True)


@extend_schema(tags=["ai"], request=ChatRequestSerializer, responses={200: ChatResponseSerializer})
class ChatView(APIView):
    """Responde una pregunta sobre las finanzas del workspace activo.

    La IA nunca toca la base directo ni genera SQL -- elige una función ya
    existente de `apps.reports.services` y sólo redacta la respuesta con lo
    que esa función devuelve. Ver `apps.ai.chat` para el detalle.
    """

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai"

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            raise ValidationError({"question": "Requerido."})
        if len(question) > chat.MAX_QUESTION_LENGTH:
            raise ValidationError({"question": f"Máximo {chat.MAX_QUESTION_LENGTH} caracteres."})

        try:
            result = chat.ask(user=request.user, workspace=request.workspace, question=question)
        except AIUnavailable:
            raise AIServiceUnavailable("No se pudo responder ahora mismo. Probá de nuevo en un momento.")

        return Response(ChatResponseSerializer(result).data)
