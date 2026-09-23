from django.contrib import admin
from django.db.models import Sum
from django.utils import timezone

from apps.common.admin import BaseModelAdmin

from .models import (
    Affiliate,
    AffiliateReferral,
    Payment,
    Plan,
    PlanPrice,
    PromoCode,
    PromoCodeRedemption,
    Subscription,
)


def _money(cents) -> str:
    return f"{(cents or 0) / 100:,.2f}"


class PlanPriceInline(admin.TabularInline):
    model = PlanPrice
    extra = 0


@admin.register(Plan)
class PlanAdmin(BaseModelAdmin):
    """Límites y features de cada plan -- editar acá se refleja en el acto
    (el cliente los lee de `/api/v1/plans/`, nunca los hardcodea)."""

    list_display = (
        "name", "code", "is_default", "trial_days",
        "max_workspaces_owned", "max_members_per_workspace", "max_active_recurring",
    )
    search_fields = ("name", "code")
    inlines = [PlanPriceInline]


@admin.register(PlanPrice)
class PlanPriceAdmin(BaseModelAdmin):
    list_display = ("plan", "billing_period", "amount", "currency", "is_active")
    list_filter = ("billing_period", "currency", "is_active", "plan")


@admin.register(Subscription)
class SubscriptionAdmin(BaseModelAdmin):
    """Alta manual de Pro (comps, soporte, pruebas): crear acá con
    ``provider=manual``, ``status=active`` y, si corresponde, una fecha en
    ``current_period_end`` -- vacío = no vence."""

    list_display = (
        "user", "plan", "status", "provider", "is_trial", "current_period_end", "created_at",
    )
    list_filter = ("status", "provider", "is_trial", "plan")
    search_fields = ("user__username", "user__email", "external_subscription_id")
    raw_id_fields = ("user", "plan", "plan_price")
    readonly_fields = BaseModelAdmin.readonly_fields + ("checkout_reference",)


@admin.register(PromoCode)
class PromoCodeAdmin(BaseModelAdmin):
    """Códigos de invitación -- crear acá uno con el plan, cuántos días de
    acceso da (vacío = no vence) y cuántas personas lo pueden usar (vacío =
    sin límite), y compartir el `code` con quien querés invitar."""

    list_display = (
        "code", "plan", "affiliate", "duration_days", "max_redemptions", "redemption_count",
        "is_active", "expires_at",
    )
    list_filter = ("is_active", "plan", "affiliate")
    search_fields = ("code", "notes")
    readonly_fields = BaseModelAdmin.readonly_fields + ("redemption_count",)


@admin.register(PromoCodeRedemption)
class PromoCodeRedemptionAdmin(BaseModelAdmin):
    """Solo lectura -- el canje lo crea `services.redeem_promo_code`, nunca
    a mano (para un alta manual sin código, usar `Subscription` directo)."""

    list_display = ("user", "promo_code", "subscription", "created_at")
    search_fields = ("user__username", "user__email", "promo_code__code")
    raw_id_fields = ("user", "promo_code", "subscription")

    def has_add_permission(self, request):
        return False


class AffiliatePromoCodeInline(admin.TabularInline):
    model = PromoCode
    fk_name = "affiliate"
    extra = 0
    fields = ("code", "plan", "duration_days", "max_redemptions", "redemption_count", "is_active", "expires_at")
    readonly_fields = ("redemption_count",)
    show_change_link = True


@admin.register(Affiliate)
class AffiliateAdmin(BaseModelAdmin):
    """Influencers. Para sumar uno: crearlo acá y agregarle un código abajo (p. ej.
    `ANA30`, plan Plus, 30 días). Lo comparte como código, o como enlace
    `https://<dominio>/?ref=ANA30`: quien se registra desde ahí queda atribuido y
    recibe el beneficio solo. Las comisiones se liquidan en Pagos (filtro por afiliado
    y "Comisión pagada: No", acción "Marcar comisión como pagada")."""

    list_display = (
        "name", "commission_percent", "commission_months", "is_active",
        "signups", "paying_customers", "revenue", "commission_pending", "commission_paid",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "contact", "promo_codes__code")
    inlines = [AffiliatePromoCodeInline]

    @admin.display(description="Registros")
    def signups(self, obj):
        return obj.referrals.count()

    @admin.display(description="Clientes que pagaron")
    def paying_customers(self, obj):
        return Payment.objects.filter(user__affiliate_referral__affiliate=obj).values("user").distinct().count()

    @admin.display(description="Facturado")
    def revenue(self, obj):
        total = Payment.objects.filter(user__affiliate_referral__affiliate=obj).aggregate(t=Sum("amount_cents"))["t"]
        return _money(total)

    @admin.display(description="Comisión pendiente")
    def commission_pending(self, obj):
        return _money(obj.payments.filter(commission_paid_at__isnull=True).aggregate(t=Sum("commission_cents"))["t"])

    @admin.display(description="Comisión pagada")
    def commission_paid(self, obj):
        return _money(obj.payments.filter(commission_paid_at__isnull=False).aggregate(t=Sum("commission_cents"))["t"])


@admin.register(AffiliateReferral)
class AffiliateReferralAdmin(BaseModelAdmin):
    """Solo lectura -- se crea al registrarse con `?ref=` o al canjear un código de afiliado."""

    list_display = ("user", "affiliate", "promo_code", "source", "created_at")
    list_filter = ("affiliate", "source")
    search_fields = ("user__username", "user__email", "affiliate__name", "promo_code__code")
    raw_id_fields = ("user", "promo_code")

    def has_add_permission(self, request):
        return False


class CommissionPaidFilter(admin.SimpleListFilter):
    title = "comisión pagada"
    parameter_name = "commission_paid"

    def lookups(self, request, model_admin):
        return (("no", "No"), ("yes", "Sí"))

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(commission_cents__gt=0, commission_paid_at__isnull=True)
        if self.value() == "yes":
            return queryset.filter(commission_paid_at__isnull=False)
        return queryset


@admin.register(Payment)
class PaymentAdmin(BaseModelAdmin):
    """Cobros confirmados por el proveedor -- los crea el webhook, nunca a mano."""

    list_display = ("paid_at", "user", "amount", "currency", "provider", "affiliate", "commission", "commission_paid_at")
    list_filter = ("provider", "affiliate", CommissionPaidFilter)
    search_fields = ("user__username", "user__email", "event_id")
    raw_id_fields = ("user", "subscription")
    date_hierarchy = "paid_at"
    actions = BaseModelAdmin.actions + ["mark_commission_paid"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Monto", ordering="amount_cents")
    def amount(self, obj):
        return _money(obj.amount_cents)

    @admin.display(description="Comisión", ordering="commission_cents")
    def commission(self, obj):
        return _money(obj.commission_cents)

    @admin.action(description="Marcar comisión como pagada")
    def mark_commission_paid(self, request, queryset):
        pending = queryset.filter(commission_cents__gt=0, commission_paid_at__isnull=True)
        total = pending.aggregate(t=Sum("commission_cents"))["t"]
        updated = pending.update(commission_paid_at=timezone.now())
        self.message_user(request, f"{updated} comisión(es) marcadas como pagadas, total {_money(total)}.")

