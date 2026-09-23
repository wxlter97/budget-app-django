"""
Resolución del plan efectivo de un usuario/workspace, y aplicación de
eventos de webhook. El resto de la app (otros apps incluidos) importa SOLO
de acá -- nunca de `models` directo -- para no repetir en cada lugar que
necesita chequear un límite la lógica de "cuál suscripción cuenta".
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
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


logger = logging.getLogger(__name__)


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


def can_add_recurring(workspace) -> bool:
    """Si activar UN recurrente más (crear uno nuevo activo, o reactivar uno
    pausado) todavía entra en `plan.max_active_recurring`. En el gratis vale
    0: no es "menos recurrentes", es la función entera fuera (ver
    `seed_billing_plans` y el directorio del 22-sep-2026 de restringir el
    plan gratis)."""
    from apps.transactions.models import RecurringExpense

    plan = plan_for_workspace(workspace)
    if plan is None or plan.max_active_recurring is None:
        return True
    current = RecurringExpense.objects.filter(workspace=workspace, is_active=True).count()
    return current < plan.max_active_recurring


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
    "calendar": "El calendario financiero es parte de Plus -- pasate a Plus para verlo.",
    "notifications": "Los avisos y recordatorios son parte de Plus -- pasate a Plus para activarlos.",
    "wallet_split": "Dividir una cartera en varias es parte de Plus -- pasate a Plus para hacerlo.",
    "transaction_duplicate": "Duplicar una transacción es parte de Plus -- pasate a Plus para usarlo.",
    "refunds": "Registrar reembolsos es parte de Plus -- pasate a Plus para hacerlo.",
    "split_categories": "Dividir una transacción entre categorías es parte de Plus -- pasate a Plus para hacerlo.",
    "split_people": "Dividir gastos entre personas es parte de Plus -- pasate a Plus para hacerlo.",
    "installments": "Las compras a plazo son parte de Plus -- pasate a Plus para registrarlas.",
    "statements": "Los estados de cuenta de tarjeta son parte de Plus -- pasate a Plus para verlos.",
    "net_worth": "El patrimonio neto es parte de Plus -- pasate a Plus para verlo.",
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


def proration_credit_cents(sub: Subscription | None) -> int:
    """
    Valor sin usar de una suscripción pagada, para acreditarlo al cambiar de plan: la
    parte proporcional del precio por el tiempo que le queda (puede ser más de un período
    si renovó antes). Cero para lo que no se pagó (alta manual, código, prueba gratis),
    lo que no tiene período (de por vida) o lo que ya venció.
    """
    if sub is None or sub.provider == PROVIDER_MANUAL or sub.is_trial or sub.plan_price is None:
        return 0
    months = _PERIOD_MONTHS.get(sub.plan_price.billing_period)
    end = sub.current_period_end
    now = timezone.now()
    if months is None or end is None or end <= now:
        return 0
    period = (end - (end - relativedelta(months=months))).total_seconds()
    return int(sub.plan_price.amount_cents * (end - now).total_seconds() / period)


def _credit_as_time(sub: Subscription) -> timedelta:
    """El crédito de prorrateo de `sub` convertido en tiempo de su plan nuevo, a su precio:
    p. ej. USD 0.66 de crédito en un plan de USD 1.99/mes son ~10 días. Sólo si la
    suscripción de la que viene el crédito sigue vigente -- si ya la reemplazó otra
    compra (dos checkouts a la vez), ese crédito ya se usó."""
    old = sub.prorated_from
    if not sub.proration_credit_cents or old is None or not old.is_in_force:
        return timedelta(0)
    months = _PERIOD_MONTHS.get(sub.plan_price.billing_period) if sub.plan_price else None
    if months is None or not sub.plan_price.amount_cents:
        return timedelta(0)  # de por vida: el crédito ya se descontó del cobro
    now = timezone.now()
    period = now + relativedelta(months=months) - now
    return period * (sub.proration_credit_cents / sub.plan_price.amount_cents)


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
            try:
                sub = Subscription.objects.filter(checkout_reference=event.reference).first()
            except (DjangoValidationError, ValueError):
                # La referencia no tiene forma de UUID -- p. ej. un enlace de prueba creado
                # con `wompi_probe --pago` (referencia "probe-<uuid>"), o cualquier otro valor
                # ajeno. No es nuestro, se ignora como cualquier referencia sin match, en vez
                # de tirar 500 -- Wompi reintenta un webhook fallido, así que sin esto un solo
                # enlace de prueba deja reintentando (y fallando) para siempre.
                sub = None
        if sub is None and event.external_subscription_id:
            sub = Subscription.objects.filter(
                provider=provider_code, external_subscription_id=event.external_subscription_id
            ).first()
        if sub is None:
            return None

        # Nunca activar por menos de lo que cuesta: un enlace con monto editable, o un aviso
        # armado a mano por quien conozca la firma, no debe regalar el plan.
        if event.amount is not None and sub.plan_price is not None:
            expected = Decimal(sub.charge_cents) / 100
            if event.amount < expected:
                logger.warning(
                    "Pago de %s por %s (esperado %s): no se activa la suscripción %s.",
                    provider_code, event.amount, expected, sub.pk,
                )
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

        was_pending = sub.status == Subscription.STATUS_PENDING
        if event.kind in ("subscription.activated", "subscription.renewed"):
            sub.status = Subscription.STATUS_ACTIVE
            sub.current_period_end = event.current_period_end or _paid_period_end(sub)
            if was_pending and sub.current_period_end is not None:
                sub.current_period_end += _credit_as_time(sub)
        elif event.kind == "subscription.failed":
            sub.status = Subscription.STATUS_PAST_DUE
        elif event.kind == "subscription.canceled":
            sub.status = Subscription.STATUS_CANCELED
            sub.canceled_at = timezone.now()

        sub.save(update_fields=[
            "status", "current_period_end", "canceled_at",
            "external_subscription_id", "external_customer_id", "updated_at",
        ])

        if event.kind in ("subscription.activated", "subscription.renewed"):
            notify_billing_event("new_subscriber" if was_pending else "renewed", sub)
        elif event.kind == "subscription.failed":
            notify_billing_event("failed", sub)

    # Fuera de la transacción: dar de baja el plan anterior puede pegarle a la API del
    # proveedor, y si eso falla la activación del plan nuevo ya tiene que haber quedado.
    if was_pending and sub.status == Subscription.STATUS_ACTIVE:
        supersede_previous_subscriptions(sub)
    return sub


def supersede_previous_subscriptions(new_sub: Subscription) -> None:
    """
    Cambio de plan: al activarse `new_sub`, cualquier otra suscripción vigente del
    mismo usuario queda cancelada ya mismo, y se le pide al proveedor que no la vuelva
    a cobrar (el enlace recurrente de Wompi). Sin esto el usuario seguiría pagando los
    dos planes, y la vieja dispararía avisos de "se renueva pronto"/"venció".

    El prorrateo ya se aplicó antes: lo que le quedaba al plan anterior se descontó del
    cobro o se sumó como tiempo al nuevo (`proration_credit_cents`). Un fallo del proveedor no deshace nada -- queda en
    el log para desactivar el enlace a mano (`wompi_probe --desactivar ID`).
    """
    from .providers import get_provider

    previous = Subscription.objects.filter(
        user=new_sub.user,
        status__in=[Subscription.STATUS_ACTIVE, Subscription.STATUS_PAST_DUE],
    ).exclude(pk=new_sub.pk).select_related("plan_price")
    for old in previous:
        if old.canceled_at is None and old.provider != PROVIDER_MANUAL:
            try:
                get_provider(old.provider).cancel_subscription(old)
            except Exception:
                logger.exception(
                    "No se pudo dar de baja en %s la suscripción %s al cambiar al plan %s: "
                    "desactivar a mano su enlace (%s).",
                    old.provider, old.pk, new_sub.plan.code, old.external_subscription_id,
                )
        old.status = Subscription.STATUS_CANCELED
        old.canceled_at = old.canceled_at or timezone.now()
        old.notes = (old.notes + "\n" if old.notes else "") + (
            f"Reemplazada por el plan {new_sub.plan.name} ({new_sub.pk})."
        )
        old.save(update_fields=["status", "canceled_at", "notes", "updated_at"])


# ---------------------------------------------------------------------------
# Avisos de pago a Discord -- ver BILLING_WEBHOOK_URL. Separado del webhook de
# soporte (SUPPORT_WEBHOOK_URL): audiencia distinta, no se quiere mezclar
# "alguien escribió un ticket" con "entró/se cayó un pago".
# ---------------------------------------------------------------------------
_BILLING_EMOJI = {"new_subscriber": "🎉", "renewed": "💳", "failed": "⚠️", "expired": "⏰"}
_BILLING_LABEL = {
    "new_subscriber": "Nueva suscripción",
    "renewed": "Renovación cobrada",
    "failed": "Cobro fallido",
    "expired": "Venció sin renovarse",
}


def notify_billing_event(kind: str, sub: Subscription) -> None:
    """Postea un evento de pago al webhook de Discord configurado
    (`BILLING_WEBHOOK_URL`). No hace nada si no hay webhook configurado, y nunca
    revienta el flujo de billing si el POST falla -- el evento real (activar,
    marcar vencida, etc.) ya se aplicó de todos modos."""
    webhook_url = settings.BILLING_WEBHOOK_URL
    if not webhook_url:
        return

    price = sub.plan_price
    amount = f"${price.amount:.2f} {price.get_billing_period_display().lower()}" if price else "—"
    content = (
        f"{_BILLING_EMOJI.get(kind, '💬')} **{_BILLING_LABEL.get(kind, kind)}** — {sub.plan.name}\n"
        f"{sub.user.email} · {amount} · {sub.provider}"
    )
    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except requests.RequestException:
        logger.warning("No se pudo notificar el evento de billing '%s' a Discord.", kind, exc_info=True)


# ---------------------------------------------------------------------------
# Recordatorios de vencimiento -- ver apps.notifications.tasks / run_daily_tasks.
# ---------------------------------------------------------------------------
RENEWAL_REMINDER_WINDOW = timedelta(days=3)


def send_renewal_reminders() -> None:
    """
    Recordatorios de vencimiento para suscripciones mensuales/anuales, y detección de
    las que vencieron sin renovarse. Wompi sólo avisa cobros EXITOSOS (ver
    `WompiProvider`) -- un cobro recurrente fallido, o un anual que nadie volvió a pagar,
    no manda ningún webhook. Esto es lo único que se entera: si `current_period_end` ya
    pasó y nadie la renovó, se marca vencida acá, no por un aviso del proveedor.

    Nada de por vida (`current_period_end` es None, nunca vence) ni suscripciones sin
    `plan_price` (no se puede saber cuánto dura un período que no se conoce).
    Idempotente por `renewal_notice_sent_for`: compara contra el `current_period_end`
    actual, así que renovar (que lo cambia) habilita un aviso nuevo para el período nuevo.
    """
    from apps.notifications.models import Notification, PushDevice
    from apps.notifications.services import notify_user, send_push

    now = timezone.now()
    candidates = (
        Subscription.objects.filter(
            status__in=[Subscription.STATUS_ACTIVE, Subscription.STATUS_PAST_DUE],
            current_period_end__isnull=False,
            plan_price__isnull=False,
        )
        .exclude(plan_price__billing_period=PlanPrice.BILLING_LIFETIME)
        .select_related("user", "plan", "plan_price")
    )
    for sub in candidates:
        if sub.renewal_notice_sent_for == sub.current_period_end:
            continue  # ya se avisó de este vencimiento puntual

        expired = sub.current_period_end <= now
        if not expired and sub.current_period_end - now > RENEWAL_REMINDER_WINDOW:
            continue  # todavía falta demasiado

        when = sub.current_period_end.strftime("%d/%m/%Y")
        if expired:
            kind = Notification.KIND_SUBSCRIPTION_EXPIRED
            title = f"Tu plan {sub.plan.name} venció"
            body = "No se renovó a tiempo. Podés volver a suscribirte cuando quieras desde Cuenta → Pro."
            sub.status = Subscription.STATUS_EXPIRED
            notify_billing_event("expired", sub)
        elif sub.plan_price.billing_period == PlanPrice.BILLING_MONTHLY:
            kind = Notification.KIND_SUBSCRIPTION_RENEWAL_DUE
            title = "Tu plan se renueva pronto"
            body = f"{sub.plan.name} se renueva el {when} por ${sub.plan_price.amount:.2f}."
        else:
            kind = Notification.KIND_SUBSCRIPTION_RENEWAL_DUE
            title = "Tu plan Pro vence pronto"
            body = f"{sub.plan.name} vence el {when}. Renovalo desde Cuenta → Pro para no perder el acceso."

        sub.renewal_notice_sent_for = sub.current_period_end
        sub.save(update_fields=["status", "renewal_notice_sent_for", "updated_at"])

        data = {"type": kind, "plan": sub.plan.code}
        notify_user(sub.user, kind=kind, title=title, body=body, data=data)
        devices = list(PushDevice.objects.filter(user=sub.user))
        if devices:
            send_push(devices, title=title, body=body, data=data)


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
