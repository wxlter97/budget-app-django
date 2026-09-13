import hashlib
import hmac

from django.conf import settings
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.models import Wallet
from apps.common.api import HasWorkspaceMembership
from apps.transactions.models import Category, Transaction
from apps.transactions.services import guess_category_by_merchant

from . import services
from .models import BankEmailSchema, EmailImportLog


# ---------------------------------------------------------------------------
# BankEmailSchema  (configuración global, no por workspace)
# ---------------------------------------------------------------------------
class BankEmailSchemaSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankEmailSchema
        fields = (
            "id", "bank_name", "sender_pattern", "parser_version", "is_active",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class IsAdminOrReadOnly(IsAuthenticated):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return bool(request.user and request.user.is_staff)


class BankEmailSchemaViewSet(viewsets.ModelViewSet):
    """
    Catálogo de bancos soportados por el importador. Lo lee cualquier usuario
    autenticado; solo staff lo modifica (agregar un banco = registro aquí +
    su parser en bank_parsers/<bank>.py).
    """

    serializer_class = BankEmailSchemaSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = BankEmailSchema.objects.all()

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.method in ("GET", "HEAD", "OPTIONS") and not self.request.user.is_staff:
            return qs.filter(is_active=True)
        return qs

    def perform_destroy(self, instance):
        instance.soft_delete()


# ---------------------------------------------------------------------------
# EmailImportLog  (por workspace; flujo de confirmación manual)
# ---------------------------------------------------------------------------
class EmailImportLogSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank_schema.bank_name", read_only=True, default=None)
    suggested_category = serializers.SerializerMethodField()
    suggested_category_name = serializers.SerializerMethodField()

    class Meta:
        model = EmailImportLog
        fields = (
            "id", "status", "bank_schema", "bank_name", "wallet",
            "raw_email_subject", "extracted_amount", "extracted_merchant",
            "extracted_date", "resulting_transaction", "error_message",
            "suggested_category", "suggested_category_name",
            "created_at",
        )
        read_only_fields = fields

    def _guess(self, log):
        # Cacheado en la instancia: category y category_name comparten la
        # misma consulta, y un mismo log solo se serializa una vez por
        # response de todos modos.
        if not hasattr(log, "_suggested_category_cache"):
            log._suggested_category_cache = guess_category_by_merchant(
                workspace=log.workspace,
                txn_type=Transaction.TYPE_EXPENSE,
                merchant=log.extracted_merchant,
            )
        return log._suggested_category_cache

    def get_suggested_category(self, log) -> str | None:
        guess = self._guess(log)
        return str(guess.id) if guess else None

    def get_suggested_category_name(self, log) -> str | None:
        guess = self._guess(log)
        return guess.name if guess else None


class ConfirmImportSerializer(serializers.Serializer):
    """
    Datos para materializar la Transaction. Los que no se envían se toman de
    los valores extraídos del correo (``category`` no se extrae: es obligatoria).
    """

    wallet = serializers.PrimaryKeyRelatedField(
        queryset=Wallet.objects.none(), required=False
    )
    # Opcional: si no viene, `confirm()` intenta adivinarla por el comercio
    # (ver `suggested_category` en el log) antes de pedirla.
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.none(), required=False, allow_null=True
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    date = serializers.DateField(required=False)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        workspace = self.context["workspace"]
        self.fields["wallet"].queryset = Wallet.objects.filter(workspace=workspace)
        self.fields["category"].queryset = Category.objects.filter(workspace=workspace)


def _resolve_pending_notification(log):
    """Saca del centro de notificaciones (de TODOS los miembros que la
    tenían) el "correo por revisar" de `log`, confirmado o rechazado."""
    from apps.notifications.models import Notification
    from apps.notifications.services import resolve_notifications

    resolve_notifications(Notification.KIND_EMAIL_IMPORT_PENDING, log.id)


class EmailImportLogViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """
    Correos bancarios procesados en el workspace activo. No se crean por API
    (los genera el pipeline de ingestión); el usuario los confirma o los
    rechaza. ``?status=pending`` para filtrar la bandeja de revisión.
    """

    serializer_class = EmailImportLogSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    queryset = EmailImportLog.objects.select_related(
        "bank_schema", "wallet", "resulting_transaction"
    ).all()

    def get_queryset(self):
        qs = super().get_queryset().filter(workspace=self.request.workspace)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        from apps.billing.services import require_feature_for_workspace

        log = self.get_object()
        # El log se crea igual en cualquier plan (ver `ingest_inbound_email`
        # -- así una workspace Free ve lo que se está perdiendo), pero
        # confirmarlo (crear la transacción de verdad) es lo que gatea.
        require_feature_for_workspace(request.workspace, "import_email")
        if log.status != EmailImportLog.STATUS_PENDING:
            raise ValidationError(f"El registro no está pendiente (status={log.status}).")

        serializer = ConfirmImportSerializer(
            data=request.data, context={"workspace": request.workspace}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        wallet = data.get("wallet") or log.wallet
        amount = data.get("amount", log.extracted_amount)
        date = data.get("date", log.extracted_date)
        description = data.get("description") or log.extracted_merchant
        category = data.get("category") or guess_category_by_merchant(
            workspace=request.workspace,
            txn_type=Transaction.TYPE_EXPENSE,
            merchant=log.extracted_merchant,
        )

        missing = [
            name for name, value in (("wallet", wallet), ("amount", amount), ("date", date))
            if value is None
        ]
        if missing:
            raise ValidationError(
                {m: "Requerido (no vino en el correo)." for m in missing}
            )
        if category is None:
            options = Category.objects.filter(
                workspace=request.workspace, type=Transaction.TYPE_EXPENSE, parent__isnull=False
            ).values("id", "name")
            raise ValidationError({
                "category": "No pude adivinar la categoría — elegí una.",
                "categories": list(options),
            })

        with db_transaction.atomic():
            txn = Transaction.objects.create(
                wallet=wallet,
                category=category,
                amount=amount,
                description=description,
                date=date,
                created_by=request.user,
                source=Transaction.SOURCE_EMAIL_IMPORT,
            )
            log.resulting_transaction = txn
            log.wallet = wallet
            log.status = EmailImportLog.STATUS_CONFIRMED
            log.save(update_fields=["resulting_transaction", "wallet", "status", "updated_at"])

        _resolve_pending_notification(log)
        return Response(self.get_serializer(log).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        log = self.get_object()
        if log.status != EmailImportLog.STATUS_PENDING:
            raise ValidationError(f"El registro no está pendiente (status={log.status}).")
        log.status = EmailImportLog.STATUS_REJECTED
        log.save(update_fields=["status", "updated_at"])
        _resolve_pending_notification(log)
        return Response(self.get_serializer(log).data)

    @action(detail=False, methods=["post"], url_path="clear-failed")
    def clear_failed(self, request):
        """
        Limpia (soft-delete) el historial de correos que no se pudieron
        parsear (``status=failed``) del workspace activo -- normalmente
        bancos sin schema todavía o un formato que cambió. No toca
        pending/confirmed/rejected.
        """
        cleared = EmailImportLog.objects.filter(
            workspace=request.workspace, status=EmailImportLog.STATUS_FAILED
        ).update(is_deleted=True)
        return Response({"cleared": cleared})


# ---------------------------------------------------------------------------
# Webhook de correo entrante
# ---------------------------------------------------------------------------
def _authenticate_webhook(request, data) -> bool:
    """
    Dos modos:
    - Mailgun: si `INBOUND_MAILGUN_SIGNING_KEY` está configurada y el payload
      trae timestamp/token/signature, se verifica el HMAC-SHA256 nativo.
    - Genérico: header `X-Inbound-Secret` == `INBOUND_WEBHOOK_SECRET`.
    """
    mailgun_key = settings.INBOUND_MAILGUN_SIGNING_KEY
    if mailgun_key and {"timestamp", "token", "signature"} <= set(data):
        expected = hmac.new(
            mailgun_key.encode(),
            f"{data['timestamp']}{data['token']}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, str(data.get("signature", "")))

    secret = settings.INBOUND_WEBHOOK_SECRET
    provided = request.headers.get("X-Inbound-Secret", "")
    return bool(secret) and hmac.compare_digest(provided, secret)


class InboundEmailSerializer(serializers.Serializer):
    to = serializers.CharField(help_text="Dirección import+<token>@... (string o lista)")
    subject = serializers.CharField(required=False, allow_blank=True)
    text = serializers.CharField(required=False, allow_blank=True)
    # El remitente viaja en el campo `from` (palabra reservada en Python, por
    # eso no aparece como field). También se aceptan los nombres de Mailgun
    # (recipient/sender/body-plain) y Postmark (To/From/TextBody).


class InboundImportResultSerializer(serializers.Serializer):
    log_id = serializers.UUIDField()
    status = serializers.CharField()


@extend_schema(
    request=InboundEmailSerializer,
    responses={202: InboundImportResultSerializer},
)
class InboundEmailWebhookView(APIView):
    """
    Recibe un correo bancario ya normalizado y genera un ``EmailImportLog``.

    Auth: header ``X-Inbound-Secret`` == ``INBOUND_WEBHOOK_SECRET``.
    Body (JSON o form-encoded); se aceptan también los nombres de campo de
    Mailgun/SendGrid/Postmark:

        {"to": "...", "from": "...", "subject": "...", "text": "..."}

    Responde 202 con ``{"log_id", "status"}`` incluso si el parseo falla
    (el log queda en estado ``failed`` para revisión).
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "inbound"

    def post(self, request):
        data = request.data
        if not _authenticate_webhook(request, data):
            raise PermissionDenied("Firma / secreto de webhook inválido o no configurado.")

        to = data.get("to") or data.get("recipient") or data.get("To")
        sender = data.get("from") or data.get("sender") or data.get("From")
        subject = data.get("subject") or data.get("Subject") or ""
        text = (
            data.get("text")
            or data.get("body-plain")
            or data.get("stripped-text")
            or data.get("TextBody")
            or ""
        )
        if not to or not sender:
            raise ValidationError("Faltan los campos 'to' y/o 'from'.")

        try:
            log = services.ingest_inbound_email(
                to=to, sender=sender, subject=subject, text=text
            )
        except services.WorkspaceNotResolved:
            raise NotFound("La dirección de destino no corresponde a ningún workspace.")

        return Response({"log_id": str(log.id), "status": log.status}, status=202)
