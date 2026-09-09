"""
Reglas de negocio de los recordatorios + el envío efectivo a Expo Push.

Se llama diario (ver ``tasks.send_daily_reminders``; en producción sin Celery
corre a mano/por Cloud Scheduler, ver DEPLOY.md §6). Reutiliza el mismo
cálculo de "qué viene" que ya usa la tarjeta PROGRAMADO
(``apps.reports.services.upcoming_scheduled``) y el de presupuesto vs. gasto
real (``budget_vs_actual``) -- nada de reimplementar la matemática de
recurrencia acá.
"""
import json
import logging
from decimal import Decimal
from urllib import request as urllib_request

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.reports.services import budget_vs_actual, upcoming_scheduled
from apps.workspaces.models import Membership

from .models import NotificationLog, NotificationPreference, PushDevice

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# Límite documentado de Expo por request.
_BATCH_SIZE = 100


def send_push(devices, *, title, body, data=None):
    """Reparte por el canal que le toca a cada dispositivo: Expo Push API
    para nativo (ios/android), Web Push (RFC 8291, cifrado contra VAPID)
    para navegador -- son protocolos completamente distintos, Expo no sabe
    nada de suscripciones web. Un dispositivo caído/vencido/con error no
    tumba el resto de los avisos del día en ninguno de los dos casos."""
    native = [d for d in devices if d.platform != PushDevice.PLATFORM_WEB]
    web = [d for d in devices if d.platform == PushDevice.PLATFORM_WEB]
    if native:
        _send_expo_push(native, title=title, body=body, data=data)
    if web:
        _send_web_push(web, title=title, body=body, data=data)


def _send_expo_push(devices, *, title, body, data):
    """POST a la Expo Push API (sin dependencias nuevas: `urllib` alcanza para
    un POST de JSON). Un token vencido o Expo caído no debe tumbar el resto
    de los avisos del día -- se loggea y se sigue."""
    tokens = [d.token for d in devices]
    if not tokens:
        return
    messages = [
        {"to": t, "title": title, "body": body, "data": data or {}, "sound": "default"}
        for t in tokens
    ]
    for i in range(0, len(messages), _BATCH_SIZE):
        batch = messages[i : i + _BATCH_SIZE]
        req = urllib_request.Request(
            EXPO_PUSH_URL,
            data=json.dumps(batch).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=10) as resp:
                resp.read()
        except OSError as exc:
            # Cubre urllib.error.URLError/HTTPError (ambas son OSError) y
            # errores de socket (timeout, DNS...) -- Expo caído no debe
            # tumbar el resto de los avisos del día.
            logger.warning("Fallo enviando push a Expo (%d tokens): %s", len(batch), exc)


def _send_web_push(devices, *, title, body, data):
    """Un `PushDevice` web guarda `token` = endpoint de la suscripción +
    `p256dh`/`auth` (las claves para cifrar el payload, ver
    `PushDeviceSerializer`). Sin VAPID configurado (`.env`, ver
    `.env.example`) no hay forma de firmar nada -- se omite con un aviso en
    vez de reventar, para no tumbar el resto de la corrida (nativo incluido)
    en un deploy que todavía no lo configuró."""
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        logger.warning(
            "Push web sin VAPID configurado (%d dispositivos) -- ver VAPID_PUBLIC_KEY/"
            "VAPID_PRIVATE_KEY en .env. Se omite.",
            len(devices),
        )
        return

    # Import diferido: sólo hace falta si de verdad hay algo que mandar por
    # este canal, y evita que el resto del proyecto dependa de que
    # `cryptography`/`pywebpush` estén perfectamente instalados para arrancar.
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    for device in devices:
        subscription_info = {
            "endpoint": device.token,
            "keys": {"p256dh": device.p256dh, "auth": device.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                timeout=10,
            )
        except WebPushException as exc:
            if exc.status_code in (404, 410):
                # La suscripción ya no existe (permiso revocado, otro
                # navegador, se limpiaron los datos del sitio...) -- no
                # vale la pena reintentar nunca más.
                device.delete()
            else:
                logger.warning("Fallo enviando push web a %s: %s", device.id, exc)
        except OSError as exc:
            logger.warning("Fallo de red enviando push web a %s: %s", device.id, exc)


def _get_preference(user) -> NotificationPreference:
    pref, _ = NotificationPreference.objects.get_or_create(user=user)
    return pref


def _devices_for(user) -> list[PushDevice]:
    return list(PushDevice.objects.filter(user=user))


def _mark_sent(user, workspace, kind, dedupe_key) -> bool:
    """Registra el aviso y devuelve True si YA se había mandado antes (no
    reenviar) -- False si esta llamada es la que recién lo registró."""
    _, created = NotificationLog.objects.get_or_create(
        user=user, kind=kind, dedupe_key=dedupe_key, defaults={"workspace": workspace}
    )
    return not created


def _active_memberships():
    return Membership.objects.select_related("user", "workspace").filter(
        workspace__is_deleted=False
    )


def _fmt_amount(amount: Decimal) -> str:
    return f"{amount:.2f}"


def notify_due_items():
    """Recurrentes y cuotas que vencen mañana."""
    tomorrow = timezone.localdate() + timezone.timedelta(days=1)

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not (pref.remind_recurring or pref.remind_installments):
            continue
        devices = _devices_for(user)
        if not devices:
            continue

        for item in upcoming_scheduled(workspace, user, since=tomorrow, until=tomorrow):
            is_recurring = item["kind"] == "recurring"
            if is_recurring and not pref.remind_recurring:
                continue
            if not is_recurring and not pref.remind_installments:
                continue

            kind = (
                NotificationLog.KIND_RECURRING_DUE
                if is_recurring
                else NotificationLog.KIND_INSTALLMENT_DUE
            )
            dedupe_key = f"{item['source_id']}:{item['date'].isoformat()}"
            if _mark_sent(user, workspace, kind, dedupe_key):
                continue

            send_push(
                devices,
                title="Gasto recurrente mañana" if is_recurring else "Cuota mañana",
                body=f"{item['description']} · {_fmt_amount(item['amount'])} — {workspace.name}",
                data={"type": kind, "workspace": str(workspace.id), "source_id": str(item["source_id"])},
            )


def notify_budget_thresholds():
    """Categorías del mes en curso que ya cruzaron el % de aviso del usuario."""
    today = timezone.localdate()

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not pref.warn_budget:
            continue
        devices = _devices_for(user)
        if not devices:
            continue

        for row in budget_vs_actual(workspace, user, today.year, today.month)["rows"]:
            budgeted = row["budgeted"] + row["provision"]
            if budgeted <= 0:
                continue
            pct = (row["spent"] / budgeted) * 100
            if pct < pref.budget_threshold_pct:
                continue

            dedupe_key = f"{row['category']}:{today.year}-{today.month:02d}"
            if _mark_sent(user, workspace, NotificationLog.KIND_BUDGET_THRESHOLD, dedupe_key):
                continue

            title = "Presupuesto superado" if pct >= 100 else "Presupuesto casi agotado"
            send_push(
                devices,
                title=title,
                body=f"{row['category_name']}: {pct:.0f}% usado — {workspace.name}",
                data={
                    "type": NotificationLog.KIND_BUDGET_THRESHOLD,
                    "workspace": str(workspace.id),
                    "category": row["category"],
                },
            )


def notify_low_balance():
    """Carteras visibles con `low_balance_threshold` fijado cuyo
    `current_balance` ya cayó por debajo. Se avisa como mucho una vez por
    mes por cartera mientras siga baja (no todos los días) -- si sube y
    vuelve a bajar, se vuelve a avisar."""
    from apps.accounts.models import Wallet

    today = timezone.localdate()

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not pref.remind_low_balance:
            continue
        devices = _devices_for(user)
        if not devices:
            continue

        wallets = Wallet.objects.filter(
            workspace=workspace, is_archived=False, low_balance_threshold__isnull=False
        ).filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        for wallet in wallets:
            if wallet.current_balance >= wallet.low_balance_threshold:
                continue

            dedupe_key = f"{wallet.id}:{today.year}-{today.month:02d}"
            if _mark_sent(user, workspace, NotificationLog.KIND_LOW_BALANCE, dedupe_key):
                continue

            send_push(
                devices,
                title="Saldo bajo",
                body=f"{wallet.name}: {_fmt_amount(wallet.current_balance)} {wallet.currency} — {workspace.name}",
                data={
                    "type": NotificationLog.KIND_LOW_BALANCE,
                    "workspace": str(workspace.id),
                    "wallet": str(wallet.id),
                },
            )


def notify_statement_due():
    """Tarjetas cuyo estado de cuenta vence dentro de
    `statement_due_days_before` días -- distinto de `notify_due_items`, que
    avisa cuota por cuota: esto es el PAGO DE CONTADO completo."""
    from apps.accounts.models import Wallet
    from apps.accounts.services import credit_card_statement

    today = timezone.localdate()

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not pref.warn_statement_due:
            continue
        devices = _devices_for(user)
        if not devices:
            continue

        wallets = (
            Wallet.objects.filter(
                workspace=workspace, kind=Wallet.KIND_CREDIT, is_archived=False
            )
            .exclude(billing_cycle_day__isnull=True)
            .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        )
        for wallet in wallets:
            statement = credit_card_statement(wallet)
            due_date = statement["payment_due_date"] if statement else None
            if due_date is None or statement["total_due"] <= 0:
                continue
            days_left = (due_date - today).days
            if not (0 <= days_left <= pref.statement_due_days_before):
                continue

            dedupe_key = f"{wallet.id}:{due_date.isoformat()}"
            if _mark_sent(user, workspace, NotificationLog.KIND_STATEMENT_DUE, dedupe_key):
                continue

            send_push(
                devices,
                title="Estado de cuenta por vencer",
                body=(
                    f"{wallet.name}: {_fmt_amount(statement['total_due'])} {wallet.currency} "
                    f"vence el {due_date.strftime('%d/%m')} — {workspace.name}"
                ),
                data={
                    "type": NotificationLog.KIND_STATEMENT_DUE,
                    "workspace": str(workspace.id),
                    "wallet": str(wallet.id),
                },
            )
