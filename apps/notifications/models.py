"""
Recordatorios push: "tu recurrente vence mañana", "esta cuota vence mañana",
"tu presupuesto de X está por agotarse". Tres piezas:

- ``PushDevice``: el token de Expo Push de cada dispositivo del usuario.
- ``NotificationPreference``: qué avisos quiere recibir (por usuario, no por
  workspace -- si sos miembro de dos presupuestos, es la misma persona
  decidiendo si le interesa el aviso).
- ``NotificationLog``: registro de lo ya avisado, para no repetir el mismo
  aviso todos los días que corra la tarea (ver ``services._mark_sent``).
"""
from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class PushDevice(BaseModel):
    PLATFORM_IOS = "ios"
    PLATFORM_ANDROID = "android"
    PLATFORM_WEB = "web"
    PLATFORM_CHOICES = [
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_ANDROID, "Android"),
        (PLATFORM_WEB, "Web"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_devices"
    )
    # Nativo (ios/android): "ExponentPushToken[...]" -- va por la Expo Push
    # API. Web: la URL `endpoint` de la suscripción (PushSubscription) --
    # ya es única por sí sola (identifica el canal push del navegador), así
    # que también sirve como `token` acá; `p256dh`/`auth` (abajo) son lo
    # que le falta para poder cifrar el payload (ver
    # `apps.notifications.services.send_push`). Único en los dos casos: un
    # dispositivo/navegador que se re-registra (reinstalar la app, otra
    # cuenta, se renovó la suscripción) simplemente reasigna el dueño en
    # vez de acumular filas muertas -- ver PushDeviceSerializer.create.
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, blank=True)
    # Solo platform=web (Web Push / RFC 8291): claves públicas de la
    # PushSubscription del navegador, para cifrar el payload contra VAPID.
    p256dh = models.CharField(max_length=255, blank=True, default="")
    auth = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user} · {self.platform or '?'}"


class NotificationPreference(BaseModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference"
    )
    remind_recurring = models.BooleanField(default=True)
    remind_installments = models.BooleanField(default=True)
    warn_budget = models.BooleanField(default=True)
    # % del presupuesto de una categoría a partir del cual avisar.
    budget_threshold_pct = models.PositiveSmallIntegerField(default=90)
    # Cartera por debajo de su `Wallet.low_balance_threshold` propio -- el
    # umbral vive en la cartera (cada una en su moneda), esto sólo prende o
    # apaga el aviso.
    remind_low_balance = models.BooleanField(default=True)
    # Vencimiento del ESTADO DE CUENTA completo de una tarjeta (distinto del
    # aviso de cuota por cuota, que ya cubre `remind_installments`).
    warn_statement_due = models.BooleanField(default=True)
    statement_due_days_before = models.PositiveSmallIntegerField(default=3)

    def __str__(self):
        return f"Preferencias de {self.user}"


class NotificationLog(BaseModel):
    KIND_RECURRING_DUE = "recurring_due"
    KIND_INSTALLMENT_DUE = "installment_due"
    KIND_BUDGET_THRESHOLD = "budget_threshold"
    KIND_LOW_BALANCE = "low_balance"
    KIND_STATEMENT_DUE = "statement_due"
    KIND_CHOICES = [
        (KIND_RECURRING_DUE, "Recurrente por vencer"),
        (KIND_INSTALLMENT_DUE, "Cuota por vencer"),
        (KIND_BUDGET_THRESHOLD, "Presupuesto por agotarse"),
        (KIND_LOW_BALANCE, "Saldo bajo"),
        (KIND_STATEMENT_DUE, "Estado de cuenta por vencer"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_logs"
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="notification_logs"
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    # Identifica la ocurrencia concreta y evita reavisar lo mismo, p. ej.
    # "<recurring_id>:2026-09-10" o "<category_id>:2026-09".
    dedupe_key = models.CharField(max_length=200)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "dedupe_key"], name="unique_notification_per_user"
            )
        ]

    def __str__(self):
        return f"{self.kind} · {self.dedupe_key} → {self.user}"


class Notification(BaseModel):
    """
    Centro de notificaciones del usuario: a diferencia de ``NotificationLog``
    (que sólo evita reavisar lo mismo dos veces, sin API), esto SÍ se lista
    en la app -- ver ``NotificationViewSet``. Cubre tanto los recordatorios
    programados (mismos ``kind`` que ``NotificationLog``, una fila por cada
    push que de verdad se manda) como cosas que hoy vivían dispersas sin
    ningún aviso centralizado: invitaciones pendientes y correos bancarios
    por revisar.
    """

    STATUS_UNREAD = "unread"
    STATUS_READ = "read"
    # Una invitación aceptada/rechazada, o un correo confirmado/rechazado
    # DESDE SU PROPIA PANTALLA (no desde acá) también tiene que dejar de
    # "pedir acción" en el centro de notificaciones -- por eso un estado
    # aparte de "leída" (ver `services.resolve_notifications`).
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_UNREAD, "No leída"),
        (STATUS_READ, "Leída"),
        (STATUS_RESOLVED, "Resuelta"),
    ]

    KIND_INVITATION = "invitation"
    KIND_EMAIL_IMPORT_PENDING = "email_import_pending"
    # Mismos strings que NotificationLog (no se redefinen): el cliente ya
    # switchea sobre `data.type` con estos valores al tocar un push (ver
    # `addNotificationTapListener`) -- tienen que seguir siendo idénticos.
    KIND_RECURRING_DUE = NotificationLog.KIND_RECURRING_DUE
    KIND_INSTALLMENT_DUE = NotificationLog.KIND_INSTALLMENT_DUE
    KIND_BUDGET_THRESHOLD = NotificationLog.KIND_BUDGET_THRESHOLD
    KIND_LOW_BALANCE = NotificationLog.KIND_LOW_BALANCE
    KIND_STATEMENT_DUE = NotificationLog.KIND_STATEMENT_DUE
    KIND_CHOICES = [
        (KIND_INVITATION, "Invitación a un presupuesto"),
        (KIND_EMAIL_IMPORT_PENDING, "Correo bancario por revisar"),
        *NotificationLog.KIND_CHOICES,
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    # Nullable: no todo tiene un workspace de origen tan claro como para
    # forzarlo (en la práctica, todos los kind de hoy sí lo tienen).
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="notifications", null=True, blank=True
    )
    kind = models.CharField(max_length=25, choices=KIND_CHOICES)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=500)
    # Payload para el tap del cliente -- mismo contrato `{"type", "workspace",
    # ...}` que ya usa el tap de un push.
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_UNREAD)
    # Id (como string) del objeto que puede resolver esta notificación desde
    # AFUERA del centro de notificaciones -- p. ej. el id de la Invitation o
    # del EmailImportLog. Texto plano en vez de un FK genérico o un lookup
    # dentro de `data` (evita depender de que el JSON key-lookup del backend
    # de base de datos en uso lo soporte igual en todos lados).
    related_object_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"{self.kind} → {self.user} ({self.status})"
