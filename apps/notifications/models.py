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
    # "ExponentPushToken[...]". Único: un dispositivo que se re-registra
    # (reinstalar la app, cambiar de cuenta) simplemente reasigna el dueño
    # en vez de acumular filas muertas -- ver PushDeviceSerializer.create.
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, blank=True)

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

    def __str__(self):
        return f"Preferencias de {self.user}"


class NotificationLog(BaseModel):
    KIND_RECURRING_DUE = "recurring_due"
    KIND_INSTALLMENT_DUE = "installment_due"
    KIND_BUDGET_THRESHOLD = "budget_threshold"
    KIND_CHOICES = [
        (KIND_RECURRING_DUE, "Recurrente por vencer"),
        (KIND_INSTALLMENT_DUE, "Cuota por vencer"),
        (KIND_BUDGET_THRESHOLD, "Presupuesto por agotarse"),
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
