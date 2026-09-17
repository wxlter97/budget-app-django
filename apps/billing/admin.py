from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Plan, PlanPrice, PromoCode, PromoCodeRedemption, Subscription


class PlanPriceInline(admin.TabularInline):
    model = PlanPrice
    extra = 0


@admin.register(Plan)
class PlanAdmin(BaseModelAdmin):
    """Límites y features de cada plan -- editar acá se refleja en el acto
    (el cliente los lee de `/api/v1/plans/`, nunca los hardcodea)."""

    list_display = (
        "name", "code", "is_default",
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
        "user", "plan", "status", "provider", "current_period_end", "created_at",
    )
    list_filter = ("status", "provider", "plan")
    search_fields = ("user__username", "user__email", "external_subscription_id")
    raw_id_fields = ("user", "plan", "plan_price")
    readonly_fields = BaseModelAdmin.readonly_fields + ("checkout_reference",)


@admin.register(PromoCode)
class PromoCodeAdmin(BaseModelAdmin):
    """Códigos de invitación -- crear acá uno con el plan, cuántos días de
    acceso da (vacío = no vence) y cuántas personas lo pueden usar (vacío =
    sin límite), y compartir el `code` con quien querés invitar."""

    list_display = (
        "code", "plan", "duration_days", "max_redemptions", "redemption_count",
        "is_active", "expires_at",
    )
    list_filter = ("is_active", "plan")
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
