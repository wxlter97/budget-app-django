"""
Planes, precios y suscripciones.

Diseño clave: el proveedor de pago (Wompi hoy, quizás otro mañana) es un
detalle de implementación. Lo único que el resto del código lee es
`Plan`/`PlanPrice` (qué límites y qué precio) y `Subscription.is_in_force`
(¿está pagando?) -- nunca nada específico de un proveedor. Ver
`apps.billing.providers` para la capa que sí sabe hablar con cada uno, y
`apps.billing.services` para la resolución de "cuál es el plan efectivo de
este usuario/workspace".

Se cobra por WORKSPACE a través de su dueño, no por asiento: el plan de un
usuario se aplica a todos los workspaces que posee, y los miembros
invitados heredan las features sin pagar aparte (ver
`services.plan_for_workspace`).
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.models import BaseModel

PROVIDER_MANUAL = "manual"
PROVIDER_WOMPI = "wompi"
PROVIDER_CHOICES = [
    (PROVIDER_MANUAL, "Manual (otorgado por admin)"),
    (PROVIDER_WOMPI, "Wompi"),
]


class Plan(BaseModel):
    """
    Un plan (gratis o pro). Vive en base de datos -- no hardcodeado en el
    cliente ni en el backend -- para poder ajustar límites y features sin
    pasar por una nueva versión de la app / revisión de las stores.
    """

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=60)
    description = models.TextField(
        blank=True, help_text="Bajada corta para la pantalla de upgrade (opcional)."
    )
    is_default = models.BooleanField(
        default=False,
        help_text="El plan que recibe un usuario sin suscripción vigente. Debe haber exactamente uno.",
    )

    # --- límites numéricos -- None = ilimitado ---
    max_workspaces_owned = models.PositiveIntegerField(
        null=True, blank=True, help_text="Workspaces que este usuario puede poseer como owner."
    )
    max_members_per_workspace = models.PositiveIntegerField(
        null=True, blank=True, help_text="Miembros (incluido el owner) por workspace de este plan."
    )
    max_active_recurring = models.PositiveIntegerField(
        null=True, blank=True, help_text="Recurrentes activos por workspace de este plan."
    )

    # --- feature flags a medida -- agregar una nueva es una key acá, sin
    # migración. Convención de claves usadas por el cliente/servicios:
    # import_email, import_excel, net_worth_history, advanced_reports,
    # export, backup, loyalty, multi_currency, quick_add.
    features = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.name

    def has_feature(self, key: str) -> bool:
        return bool(self.features.get(key))


class PlanPrice(BaseModel):
    """
    Un precio cobrable de un plan (mensual / anual / de por vida). Un mismo
    precio puede habilitarse para más de un proveedor sin duplicar filas --
    `external_refs` guarda el id que cada proveedor usa para identificarlo
    (p. ej. {"wompi": "plan_abc123"}); un proveedor sin entrada ahí todavía
    no puede cobrar este precio.
    """

    BILLING_MONTHLY = "monthly"
    BILLING_ANNUAL = "annual"
    BILLING_LIFETIME = "lifetime"
    BILLING_CHOICES = [
        (BILLING_MONTHLY, "Mensual"),
        (BILLING_ANNUAL, "Anual"),
        (BILLING_LIFETIME, "De por vida"),
    ]

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="prices")
    billing_period = models.CharField(max_length=10, choices=BILLING_CHOICES)
    amount_cents = models.PositiveIntegerField(help_text="Precio en centavos, p. ej. 1999 = 19.99.")
    currency = models.CharField(max_length=3, default="USD")
    external_refs = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("plan", "billing_period")
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "billing_period", "currency"],
                name="unique_price_per_plan_period_currency",
            )
        ]

    def __str__(self):
        return f"{self.plan.code} · {self.get_billing_period_display()} · {self.amount:.2f} {self.currency}"

    @property
    def amount(self) -> float:
        return self.amount_cents / 100

    def external_ref(self, provider_code: str) -> str:
        return self.external_refs.get(provider_code, "")


class Subscription(BaseModel):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_PAST_DUE = "past_due"
    STATUS_CANCELED = "canceled"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente de confirmación"),
        (STATUS_ACTIVE, "Activa"),
        (STATUS_PAST_DUE, "Pago vencido"),
        (STATUS_CANCELED, "Cancelada"),
        (STATUS_EXPIRED, "Expirada"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    plan_price = models.ForeignKey(
        PlanPrice, on_delete=models.PROTECT, related_name="subscriptions", null=True, blank=True
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_MANUAL)

    # Generado acá, ANTES de mandar al usuario al checkout del proveedor, y
    # pasado como referencia/metadata -- así el primer webhook que avisa
    # "esto se pagó" se puede correlacionar con esta fila aunque todavía no
    # sepamos el id que el proveedor le puso a su lado (ver
    # `services.apply_webhook_event`).
    checkout_reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    external_customer_id = models.CharField(max_length=120, blank=True)
    external_subscription_id = models.CharField(max_length=120, blank=True, db_index=True)

    current_period_end = models.DateTimeField(
        null=True, blank=True,
        help_text="Vacío = no vence (alta manual sin fecha, o plan de por vida).",
    )
    canceled_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Uso interno -- p. ej. motivo de un alta manual.")

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"{self.user} · {self.plan.code} · {self.get_status_display()}"

    @property
    def is_in_force(self) -> bool:
        """Activa o en gracia por pago vencido, y sin vencer (si tiene fecha)."""
        if self.status not in (self.STATUS_ACTIVE, self.STATUS_PAST_DUE):
            return False
        if self.current_period_end is None:
            return True
        return self.current_period_end >= timezone.now()
