"""
Resolución del plan efectivo de un usuario/workspace, y aplicación de
eventos de webhook. El resto de la app (otros apps incluidos) importa SOLO
de acá -- nunca de `models` directo -- para no repetir en cada lugar que
necesita chequear un límite la lógica de "cuál suscripción cuenta".
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from dateutil.relativedelta import relativedelta

from .models import (
    PROVIDER_MANUAL,
    Plan,
    PlanPrice,
    ProcessedWebhookEvent,
    PromoCode,
    PromoCodeRedemption,
    Subscription,
)
from .providers import WebhookEvent


def get_default_plan() -> Plan | None:
    """
    El plan default (``is_default=True``), o None si todavía no se corrió
    ``seed_billing_plans`` en este entorno. Deliberadamente NO levanta
    excepción: un entorno sin planes configurados no debe tirar 500 en cada
    creación de workspace -- los chequeos de límite (`can_own_another_workspace`,
    `can_add_member`) tratan "sin plan" como "sin límite" (fail-open).
    """
    return Plan.objects.filter(is_default=True).first()


def active_subscription_for(user) -> Subscription | None:
    """La suscripción vigente del usuario (activa o en gracia por pago
    vencido, sin vencer todavía), o None si no tiene ninguna."""
    candidates = (
        Subscription.objects.filter(
            user=user, status__in=[Subscription.STATUS_ACTIVE, Subscription.STATUS_PAST_DUE]
        )
        .select_related("plan", "plan_price")
        .order_by("-created_at")
    )
    for sub in candidates:
        if sub.is_in_force:
            return sub
    return None


def plan_for_user(user) -> Plan | None:
    sub = active_subscription_for(user)
    return sub.plan if sub else get_default_plan()


def plan_for_workspace(workspace) -> Plan | None:
    """
    Se cobra por workspace a través de su dueño, no por asiento: el plan
    efectivo del workspace es el del usuario Owner -- los miembros
    invitados heredan las features sin pagar aparte. Import diferido de
    `Membership` para evitar un ciclo apps.billing <-> apps.workspaces
    (mismo patrón que `apps.common.api.resolve_workspace`).
    """
    from apps.workspaces.models import Membership

    owner_membership = (
        workspace.memberships.filter(role=Membership.ROLE_OWNER, is_deleted=False)
        .select_related("user")
        .first()
    )
    if owner_membership is None:
        return get_default_plan()
    return plan_for_user(owner_membership.user)


# ---------------------------------------------------------------------------
# Chequeos de límite -- lo que usan los endpoints de otras apps para gatear
# ---------------------------------------------------------------------------
def can_own_another_workspace(user) -> bool:
    from apps.workspaces.models import Membership

    plan = plan_for_user(user)
    if plan is None or plan.max_workspaces_owned is None:
        return True
    owned = Membership.objects.filter(
        user=user, role=Membership.ROLE_OWNER, is_deleted=False, workspace__is_deleted=False
    ).count()
    return owned < plan.max_workspaces_owned


def can_add_member(workspace) -> bool:
    plan = plan_for_workspace(workspace)
    if plan is None or plan.max_members_per_workspace is None:
        return True
    current = workspace.memberships.filter(is_deleted=False).count()
    return current < plan.max_members_per_workspace


# ---------------------------------------------------------------------------
# Feature flags -- convención de claves en `Plan.features`, ver `models.py`
# y `seed_billing_plans`. Mismo fail-open que los límites de arriba: sin
# plan configurado (entorno sin seedear), la feature se considera habilitada.
# ---------------------------------------------------------------------------
_DEFAULT_UPGRADE_MESSAGE = "Esta función es parte del plan Pro -- pasate a Pro para activarla."

FEATURE_UPGRADE_MESSAGES = {
    "import_email": "La importación automática de correos bancarios es una función Pro -- pasate a Pro para activarla.",
    "import_excel": "Importar desde Excel es una función Pro -- pasate a Pro para activarla.",
    "net_worth_history": "El historial de patrimonio neto es una función Pro -- pasate a Pro para verlo.",
    "advanced_reports": "Tendencias y flujo de caja son funciones Pro -- pasate a Pro para verlas.",
    "export": "Exportar tus datos es una función Pro -- pasate a Pro para hacerlo.",
    "backup": "El respaldo y restauración son funciones Pro -- pasate a Pro para usarlas.",
    "loyalty": "El seguimiento de puntos y cashback es una función Pro -- pasate a Pro para activarlo.",
    "multi_currency": "Múltiples monedas con conversión es una función Pro -- pasate a Pro para activarla.",
    "quick_add": "Los atajos de carga rápida son una función Pro -- pasate a Pro para crear uno.",
}


def has_feature(user, key: str) -> bool:
    plan = plan_for_user(user)
    if plan is None:
        return True
    return plan.has_feature(key)


def has_feature_for_workspace(workspace, key: str) -> bool:
    plan = plan_for_workspace(workspace)
    if plan is None:
        return True
    return plan.has_feature(key)


def require_feature(user, key: str) -> None:
    """Levanta `PermissionDenied` (403) con el mensaje de upsell estándar
    si el plan efectivo del usuario no tiene `key` habilitada."""
    if not has_feature(user, key):
        raise PermissionDenied(FEATURE_UPGRADE_MESSAGES.get(key, _DEFAULT_UPGRADE_MESSAGE))


def require_feature_for_workspace(workspace, key: str) -> None:
    if not has_feature_for_workspace(workspace, key):
        raise PermissionDenied(FEATURE_UPGRADE_MESSAGES.get(key, _DEFAULT_UPGRADE_MESSAGE))


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
_PERIOD_MONTHS = {PlanPrice.BILLING_MONTHLY: 1, PlanPrice.BILLING_ANNUAL: 12}


def _paid_period_end(sub: Subscription):
    """
    Hasta cuándo queda pagada `sub` tras un cobro nuevo: un período más, contado desde
    lo que ya tenía pagado si todavía no vencía (renovar antes no pierde días) o desde
    hoy si ya había vencido. Sin `plan_price` no se sabe la duración: se deja como está.
    """
    if sub.plan_price is None:
        return sub.current_period_end
    months = _PERIOD_MONTHS.get(sub.plan_price.billing_period)
    if months is None:
        return None  # de por vida: no vence
    now = timezone.now()
    base = sub.current_period_end if sub.current_period_end and sub.current_period_end > now else now
    return base + relativedelta(months=months)


def apply_webhook_event(event: WebhookEvent, *, provider_code: str) -> Subscription | None:
    """
    Aplica un `WebhookEvent` ya normalizado a la Subscription
    correspondiente. Busca primero por `checkout_reference` (funciona
    incluso antes de conocer el id del proveedor, en el primer evento) y
    si no, por `external_subscription_id` (renovaciones/cancelaciones
    posteriores). Sin ninguna coincidencia, no hace nada y devuelve None
    -- p. ej. un evento de prueba del lado del proveedor.

    Idempotente por `event.event_id`: los proveedores reintentan los avisos, y aplicar dos
    veces el mismo cobro extendería el período de más.
    """
    if event.kind == "payment.ignored":
        return None

    with transaction.atomic():
        sub = None
        if event.reference:
            sub = Subscription.objects.filter(checkout_reference=event.reference).first()
        if sub is None and event.external_subscription_id:
            sub = Subscription.objects.filter(
                provider=provider_code, external_subscription_id=event.external_subscription_id
            ).first()
        if sub is None:
            return None

        if event.event_id:
            try:
                with transaction.atomic():
                    ProcessedWebhookEvent.objects.create(
                        provider=provider_code, event_id=event.event_id
                    )
            except IntegrityError:
                return sub  # ya se aplicó este aviso

        if event.external_subscription_id:
            sub.external_subscription_id = event.external_subscription_id
        if event.external_customer_id:
            sub.external_customer_id = event.external_customer_id

        if event.kind in ("subscription.activated", "subscription.renewed"):
            sub.status = Subscription.STATUS_ACTIVE
            sub.current_period_end = event.current_period_end or _paid_period_end(sub)
        elif event.kind == "subscription.failed":
            sub.status = Subscription.STATUS_PAST_DUE
        elif event.kind == "subscription.canceled":
            sub.status = Subscription.STATUS_CANCELED
            sub.canceled_at = timezone.now()

        sub.save(update_fields=[
            "status", "current_period_end", "canceled_at",
            "external_subscription_id", "external_customer_id", "updated_at",
        ])
        return sub


# ---------------------------------------------------------------------------
# Códigos de invitación (acceso gratis, autoservicio)
# ---------------------------------------------------------------------------
def redeem_promo_code(user, code: str) -> Subscription:
    """
    Canjea un código de invitación: crea una `Subscription` activa al plan
    del código (``provider=manual``, sin `plan_price` -- no hay cobro) y
    registra el canje. Levanta `ValidationError` (400) si el código no
    existe, no está vigente, ya se agotó, o el usuario ya canjeó uno antes.

    `select_for_update` sobre la fila del código: sin esto, dos requests
    concurrentes contra el último cupo de un código con `max_redemptions`
    podrían leer el mismo `redemption_count` y ambas pasar el chequeo,
    dejando el código con más canjes de los permitidos.
    """
    normalized = code.strip().upper()
    if not normalized:
        raise ValidationError({"code": "Ingresá un código."})

    with transaction.atomic():
        promo = (
            PromoCode.objects.select_for_update()
            .select_related("plan")
            .filter(code=normalized)
            .first()
        )
        if promo is None or not promo.is_redeemable:
            raise ValidationError({"code": "Código inválido o vencido."})
        if PromoCodeRedemption.objects.filter(user=user).exists():
            raise ValidationError({"code": "Ya canjeaste un código de invitación antes."})

        current_period_end = (
            timezone.now() + timedelta(days=promo.duration_days)
            if promo.duration_days is not None
            else None
        )
        subscription = Subscription.objects.create(
            user=user, plan=promo.plan, status=Subscription.STATUS_ACTIVE,
            provider=PROVIDER_MANUAL, current_period_end=current_period_end,
            notes=f"Código de invitación: {promo.code}",
        )
        PromoCodeRedemption.objects.create(promo_code=promo, user=user, subscription=subscription)
        promo.redemption_count = F("redemption_count") + 1
        promo.save(update_fields=["redemption_count", "updated_at"])

    return subscription


# ---------------------------------------------------------------------------
# Período de prueba (acceso gratis autoservicio, sin código)
# ---------------------------------------------------------------------------
def start_trial(user, plan: Plan) -> Subscription:
    """
    Arranca la prueba gratis configurada en ``plan.trial_days``, sin pasar
    por ningún proveedor de pago ni código -- se elige el plan directo
    desde el catálogo. Un usuario sólo puede empezar una prueba en toda su
    vida (sin importar qué plan haya elegido, ni si la dejó vencer o la
    canceló) -- lo garantiza también la constraint única de
    ``Subscription.Meta`` (``one_trial_subscription_per_user``), por si dos
    requests concurrentes (doble tap) pasan el chequeo de acá a la vez.
    """
    if not plan.trial_days:
        raise ValidationError({"plan": "Este plan no tiene período de prueba."})
    if Subscription.objects.filter(user=user, is_trial=True).exists():
        raise ValidationError({"plan": "Ya usaste tu período de prueba gratis."})
    if active_subscription_for(user) is not None:
        raise ValidationError({"plan": "Ya tenés una suscripción activa."})

    try:
        with transaction.atomic():
            return Subscription.objects.create(
                user=user, plan=plan, status=Subscription.STATUS_ACTIVE,
                provider=PROVIDER_MANUAL, is_trial=True,
                current_period_end=timezone.now() + timedelta(days=plan.trial_days),
                notes=f"Prueba gratis de {plan.trial_days} días.",
            )
    except IntegrityError:
        raise ValidationError({"plan": "Ya usaste tu período de prueba gratis."})
