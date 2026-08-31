import hmac

from django.conf import settings
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Account
from apps.common.api import HasWorkspaceMembership
from apps.transactions.models import Category, Transaction

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

    class Meta:
        model = EmailImportLog
        fields = (
            "id", "status", "bank_schema", "bank_name", "account",
            "raw_email_subject", "extracted_amount", "extracted_merchant",
            "extracted_date", "resulting_transaction", "error_message",
            "created_at",
        )
        read_only_fields = fields


class ConfirmImportSerializer(serializers.Serializer):
    """
    Datos para materializar la Transaction. Los que no se envían se toman de
    los valores extraídos del correo (``category`` no se extrae: es obligatoria).
    """

    account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.none(), required=False
    )
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.none())
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    date = serializers.DateField(required=False)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        workspace = self.context["workspace"]
        self.fields["account"].queryset = Account.objects.filter(workspace=workspace)
        self.fields["category"].queryset = Category.objects.filter(workspace=workspace)


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
        "bank_schema", "account", "resulting_transaction"
    ).all()

    def get_queryset(self):
        qs = super().get_queryset().filter(workspace=self.request.workspace)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        log = self.get_object()
        if log.status != EmailImportLog.STATUS_PENDING:
            raise ValidationError(f"El registro no está pendiente (status={log.status}).")

        serializer = ConfirmImportSerializer(
            data=request.data, context={"workspace": request.workspace}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        account = data.get("account") or log.account
        amount = data.get("amount", log.extracted_amount)
        date = data.get("date", log.extracted_date)
        description = data.get("description") or log.extracted_merchant

        missing = [
            name for name, value in (("account", account), ("amount", amount), ("date", date))
            if value is None
        ]
        if missing:
            raise ValidationError(
                {m: "Requerido (no vino en el correo)." for m in missing}
            )

        with db_transaction.atomic():
            txn = Transaction.objects.create(
                account=account,
                category=data["category"],
                amount=amount,
                description=description,
                date=date,
                created_by=request.user,
                source=Transaction.SOURCE_EMAIL_IMPORT,
            )
            log.resulting_transaction = txn
            log.account = account
            log.status = EmailImportLog.STATUS_CONFIRMED
            log.save(update_fields=["resulting_transaction", "account", "status", "updated_at"])

        return Response(self.get_serializer(log).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        log = self.get_object()
        if log.status != EmailImportLog.STATUS_PENDING:
            raise ValidationError(f"El registro no está pendiente (status={log.status}).")
        log.status = EmailImportLog.STATUS_REJECTED
        log.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(log).data)


# ---------------------------------------------------------------------------
# Webhook de correo entrante
# ---------------------------------------------------------------------------
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

    def post(self, request):
        secret = settings.INBOUND_WEBHOOK_SECRET
        provided = request.headers.get("X-Inbound-Secret", "")
        if not secret or not hmac.compare_digest(provided, secret):
            raise PermissionDenied("Secreto de webhook inválido o no configurado.")

        data = request.data
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
