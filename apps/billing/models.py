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
    Un plan (gratis, plus o pro -- cualquier cantidad, no hay nada
    hardcodeado a dos). Vive en base de datos -- no hardcodeado en el
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

    trial_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Días de prueba gratis de este plan sin pasar por el proveedor de pago "
                   "(ver `services.start_trial`). Vacío o 0 = sin prueba gratis.",
    )

    # --- feature flags a medida -- agregar una nueva es una key acá, sin
    # migración. Convención de claves usadas por el cliente/servicios:
    # import_email, import_excel, net_worth_history, advanced_reports,
    # export, backup, loyalty, multi_currency, quick_add.
    # Además, las cuotas mensuales de IA, que son números y no booleanos:
    # ai_receipts_per_month, ai_parses_per_month, ai_chats_per_month
    # (ver `apps.ai.quotas`; ausente = se aplica el número del plan gratis,
    # null explícito = sin tope).
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


# Cobro mínimo de un plan de por vida con crédito de prorrateo -- ver `Subscription.charge_cents`.
MIN_CHARGE_CENTS = 100


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
    is_trial = models.BooleanField(
        default=False,
        help_text="Otorgada por `services.start_trial` (Plan.trial_days), no por pago ni "
                   "código de invitación. Un usuario sólo puede tener una en toda su vida.",
    )

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

    # `current_period_end` para el que ya se mandó el recordatorio de vencimiento (o el
    # aviso de que venció sin renovarse). Comparar contra el `current_period_end` actual
    # dice si ya se avisó DE ESE período o si es uno nuevo (p. ej. tras renovar) -- ver
    # `apps.billing.services.send_renewal_reminders`.
    renewal_notice_sent_for = models.DateTimeField(null=True, blank=True)

    # Cambio de plan con prorrateo (ver `services.proration_credit_cents`): la suscripción
    # que se reemplaza y el valor sin usar que le quedaba al momento del checkout. Se
    # descuenta del precio (plan de por vida) o se suma como tiempo extra (mensual/anual).
    prorated_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    proration_credit_cents = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "status"])]
        constraints = [
            # Un usuario sólo puede tener UNA prueba gratis en toda su
            # vida -- respaldo a nivel de base de datos del chequeo de
            # `services.start_trial`, por si dos requests concurrentes
            # (doble tap) pasan el chequeo de la app a la vez.
            models.UniqueConstraint(
                fields=["user"], condition=models.Q(is_trial=True),
                name="one_trial_subscription_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.plan.code} · {self.get_status_display()}"

    @property
    def charge_cents(self) -> int:
        """Lo que se cobra por esta suscripción. Igual al precio, salvo un plan de por vida
        comprado con crédito de un plan anterior: ahí se descuenta, con un piso de
        `MIN_CHARGE_CENTS` (un enlace de pago no puede ser de 0). En mensual/anual el
        crédito no toca el cobro -- se suma como tiempo al activarse."""
        if self.plan_price is None:
            return 0
        amount = self.plan_price.amount_cents
        if self.plan_price.billing_period == PlanPrice.BILLING_LIFETIME and self.proration_credit_cents:
            return max(amount - self.proration_credit_cents, min(amount, MIN_CHARGE_CENTS))
        return amount

    @property
    def is_in_force(self) -> bool:
        """Activa o en gracia por pago vencido, y sin vencer (si tiene fecha)."""
        if self.status not in (self.STATUS_ACTIVE, self.STATUS_PAST_DUE):
            return False
        if self.current_period_end is None:
            return True
        return self.current_period_end >= timezone.now()


class Affiliate(BaseModel):
    """
    Influencer o socio que trae clientes. Sus códigos (`PromoCode.affiliate`) dan el
    beneficio a quien llega y, además, dejan anotado de quién vino (`AffiliateReferral`).
    La comisión se calcula en cada pago de esos clientes (`Payment.commission_cents`),
    y se paga a mano -- Wompi no hace pagos salientes.
    """

    name = models.CharField(max_length=120)
    contact = models.CharField(max_length=200, blank=True, help_text="Correo, @usuario, teléfono...")
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=20,
        help_text="Porcentaje de cada pago de sus referidos, p. ej. 20 = 20 %.",
    )
    commission_months = models.PositiveIntegerField(
        null=True, blank=True, default=6,
        help_text="Durante cuántos meses desde el primer pago de cada referido se paga "
                   "comisión. Vacío = siempre.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Inactivo: sus códigos siguen dando el beneficio, pero no generan comisión nueva.",
    )
    notes = models.TextField(blank=True, help_text="Uso interno -- acuerdo, forma de pago...")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class PromoCode(BaseModel):
    """
    Código de invitación: da acceso gratis a un plan sin pasar por ningún
    proveedor de pago (crea una `Subscription` con ``provider=manual``,
    igual que un alta manual del admin, pero autoservicio -- el usuario lo
    canjea él mismo en vez de que un admin le cree la suscripción a mano).

    Pensado para invitar a los primeros usuarios a probar sin pagar, no
    para descuentos en el checkout real (que hoy no existe -- ver
    `WompiProvider`, sin terminar).
    """

    code = models.CharField(
        max_length=40, unique=True,
        help_text="Sin distinguir mayúsculas/minúsculas al canjear -- se guarda en mayúsculas.",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="promo_codes")
    duration_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Días de acceso desde que se canjea. Vacío = no vence (mientras dure la beta).",
    )
    max_redemptions = models.PositiveIntegerField(
        null=True, blank=True, help_text="Cuántas personas distintas pueden usarlo. Vacío = sin límite.",
    )
    redemption_count = models.PositiveIntegerField(default=0, editable=False)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Fecha límite para CANJEAR el código (no confundir con `duration_days`, "
                   "que es cuánto dura el acceso ya canjeado). Vacío = sin fecha límite.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, help_text="Uso interno -- p. ej. a quién se le mandó.")
    affiliate = models.ForeignKey(
        Affiliate, on_delete=models.PROTECT, null=True, blank=True, related_name="promo_codes",
        help_text="Código de un influencer: quien lo use (o llegue por su enlace ?ref=CÓDIGO) "
                   "queda atribuido a él.",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    @property
    def is_redeemable(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at is not None and self.expires_at < timezone.now():
            return False
        if self.max_redemptions is not None and self.redemption_count >= self.max_redemptions:
            return False
        return True


class PromoCodeRedemption(BaseModel):
    """
    Quién canjeó qué código. Un usuario sólo puede canjear un código en toda
    su vida (no por código): así nadie encadena varias invitaciones para
    extender el acceso gratis indefinidamente -- ver `services.redeem_promo_code`.
    """

    promo_code = models.ForeignKey(PromoCode, on_delete=models.PROTECT, related_name="redemptions")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="promo_code_redemption"
    )
    subscription = models.OneToOneField(
        Subscription, on_delete=models.CASCADE, related_name="promo_code_redemption"
    )

    def __str__(self):
        return f"{self.user} · {self.promo_code.code}"


class ProcessedWebhookEvent(models.Model):
    """
    Avisos de proveedor ya aplicados. Los proveedores reintentan los webhooks: sin este
    registro, un aviso repetido volvería a extender el período de una suscripción (y
    cobraría "dos meses" por un solo pago). Sin soft delete a propósito: borrarlo
    reabriría la puerta al doble conteo.
    """

    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="unique_webhook_event_per_provider"
            )
        ]

    def __str__(self):
        return f"{self.provider}:{self.event_id}"


class AffiliateReferral(BaseModel):
    """
    De qué influencer vino un usuario. Primer contacto gana: una vez atribuido, otro
    código u otro enlace no lo cambia.
    """

    SOURCE_LINK = "link"
    SOURCE_CODE = "code"
    SOURCE_CHOICES = [(SOURCE_LINK, "Enlace (?ref=)"), (SOURCE_CODE, "Código canjeado")]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="affiliate_referral"
    )
    affiliate = models.ForeignKey(Affiliate, on_delete=models.PROTECT, related_name="referrals")
    promo_code = models.ForeignKey(
        PromoCode, on_delete=models.SET_NULL, null=True, blank=True, related_name="referrals"
    )
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user} · {self.affiliate}"


class Payment(BaseModel):
    """
    Cada cobro confirmado por un proveedor (primer pago y renovaciones), con la comisión
    de afiliado que le corresponde, calculada al momento del pago. Antes sólo se sabía
    hasta cuándo estaba pagada una suscripción, no cuánto ni cuándo se cobró.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=120, blank=True)
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    paid_at = models.DateTimeField(default=timezone.now)

    affiliate = models.ForeignKey(
        Affiliate, on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    commission_cents = models.PositiveIntegerField(default=0)
    commission_paid_at = models.DateTimeField(
        null=True, blank=True, help_text="Cuándo se le pagó esta comisión al afiliado.",
    )

    class Meta:
        ordering = ("-paid_at",)
        indexes = [models.Index(fields=["affiliate", "commission_paid_at"])]

    def __str__(self):
        return f"{self.user} · {self.amount_cents / 100:.2f} {self.currency}"
