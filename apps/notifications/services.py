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

from apps.common import periods
from apps.reports.services import behavior_insights, budget_vs_actual, upcoming_scheduled
from apps.workspaces.models import Membership

from .models import Notification, NotificationLog, NotificationPreference, PushDevice

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# Límite documentado de Expo por request.
_BATCH_SIZE = 100
# Día en que se calculan los patrones de gasto (0 = lunes), ver `notify_insights`.
INSIGHTS_WEEKDAY = 0


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
    en un deploy que todavía no lo configuró.

    Devuelve un resultado por dispositivo (`{"device", "ok", "status", "detail"}`):
    los avisos programados lo ignoran, pero el "aviso de prueba" (`send_test_push`)
    lo muestra: un push que el servicio de Apple/Google acepta y el navegador no
    muestra es indistinguible desde acá, así que al menos se ve que el servidor sí salió."""
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        logger.warning(
            "Push web sin VAPID configurado (%d dispositivos) -- ver VAPID_PUBLIC_KEY/"
            "VAPID_PRIVATE_KEY en .env. Se omite.",
            len(devices),
        )
        return [
            {"device": str(d.id), "ok": False, "status": None, "detail": "Servidor sin claves VAPID."}
            for d in devices
        ]

    # Import diferido: sólo hace falta si de verdad hay algo que mandar por
    # este canal, y evita que el resto del proyecto dependa de que
    # `cryptography`/`pywebpush` estén perfectamente instalados para arrancar.
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    results = []
    for device in devices:
        subscription_info = {
            "endpoint": device.token,
            "keys": {"p256dh": device.p256dh, "auth": device.auth},
        }
        result = {"device": str(device.id), "ok": True, "status": None, "detail": "Entregado al servicio de push."}
        try:
            response = webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                timeout=10,
            )
            result["status"] = getattr(response, "status_code", None)
        except WebPushException as exc:
            status = exc.response.status_code if getattr(exc, "response", None) is not None else None
            result.update(ok=False, status=status, detail=str(exc)[:200])
            if status in (404, 410):
                # La suscripción ya no existe (permiso revocado, otro
                # navegador, se limpiaron los datos del sitio...) -- no
                # vale la pena reintentar nunca más.
                device.delete()
                result["detail"] = "La suscripción ya no existe (se eliminó); hay que volver a activarla."
            else:
                logger.warning("Fallo enviando push web a %s: %s", device.id, exc)
        except OSError as exc:
            logger.warning("Fallo de red enviando push web a %s: %s", device.id, exc)
            result.update(ok=False, detail=f"Fallo de red: {exc}"[:200])
        results.append(result)
    return results


def send_test_push(user) -> list[dict]:
    """Manda un aviso de prueba a todos los dispositivos del usuario y devuelve
    qué pasó con cada uno (ver `_send_web_push`). Los nativos (Expo) sólo se
    reportan como enviados: Expo responde de forma asíncrona."""
    devices = _devices_for(user)
    web = [d for d in devices if d.platform == PushDevice.PLATFORM_WEB]
    native = [d for d in devices if d.platform != PushDevice.PLATFORM_WEB]
    title, body = "Aviso de prueba", "Si ves esto, los avisos de este dispositivo funcionan."
    results = _send_web_push(web, title=title, body=body, data={"type": "test"}) if web else []
    if native:
        _send_expo_push(native, title=title, body=body, data={"type": "test"})
        results += [
            {"device": str(d.id), "ok": True, "status": None, "detail": "Enviado a Expo."}
            for d in native
        ]
    return results


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


def notify_user(
    user, *, kind, title, body, workspace=None, data=None, related_object_id=""
) -> Notification:
    """Crea la fila en el centro de notificaciones del usuario (ver
    ``NotificationViewSet``). No manda push por sí sola -- eso lo decide
    cada caller (ver ``_notify`` para los recordatorios programados, que sí
    lo hacen cuando hay dispositivos registrados)."""
    return Notification.objects.create(
        user=user,
        workspace=workspace,
        kind=kind,
        title=title,
        body=body,
        data=data or {},
        related_object_id=str(related_object_id) if related_object_id else "",
    )


def resolve_notifications(kind, related_object_id, *, users=None):
    """Marca como resueltas las notificaciones de ``kind`` ligadas a
    ``related_object_id`` -- p. ej. al aceptar/rechazar una invitación, o
    confirmar/rechazar un correo bancario DESDE SU PROPIA PANTALLA (no
    desde el centro de notificaciones). ``users`` filtra a quién; sin
    especificar, resuelve para cualquiera que tuviera una (p. ej. cualquier
    miembro del workspace, para un correo bancario que le llegó a todos)."""
    qs = Notification.objects.filter(
        kind=kind, related_object_id=str(related_object_id)
    ).exclude(status=Notification.STATUS_RESOLVED)
    if users is not None:
        qs = qs.filter(user__in=users)
    qs.update(status=Notification.STATUS_RESOLVED)


def _notify(user, workspace, kind, dedupe_key, *, title, body, data, devices):
    """Registra el aviso (una vez por ocurrencia, ver ``_mark_sent``), crea
    la fila del centro de notificaciones, y lo manda por push si el usuario
    tiene algún dispositivo registrado -- las tres cosas juntas para no
    repetir esta secuencia en cada ``notify_*`` de abajo. A propósito NO
    depende de que haya dispositivos: sin ninguno, igual queda visible en
    la app -- sólo se omite el push en sí.

    Gatea la feature "notifications" del plan del workspace -- pero SÓLO
    acá, no en ``notify_user`` (genérico, lo usan también invitaciones y
    avisos de suscripción, que no son "recordatorios" y siguen en el
    gratis). Sin ``_mark_sent`` (deduplicación) de por medio: un workspace
    sin la feature nunca deja fila en ``NotificationLog`` para no dejar un
    "ya se mandó" fantasma si más tarde se pasa a un plan que sí la tiene."""
    from apps.billing.services import has_feature_for_workspace

    if workspace is not None and not has_feature_for_workspace(workspace, "notifications"):
        return
    if _mark_sent(user, workspace, kind, dedupe_key):
        return
    notify_user(user, kind=kind, title=title, body=body, workspace=workspace, data=data)
    if devices:
        send_push(devices, title=title, body=body, data=data)


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
            # Un recurrente puede ser income/expense/transfer -- "Gasto
            # recurrente mañana" quedaba mal para un sueldo o un aporte
            # automático a otra cartera (ver `item["type"]`, `upcoming_scheduled`).
            if is_recurring:
                title = {
                    "income": "Ingreso recurrente mañana",
                    "transfer": "Transferencia recurrente mañana",
                }.get(item["type"], "Gasto recurrente mañana")
            else:
                title = "Cuota mañana"
            _notify(
                user, workspace, kind, dedupe_key,
                title=title,
                body=f"{item['description']} · {_fmt_amount(item['amount'])} — {workspace.name}",
                data={"type": kind, "workspace": str(workspace.id), "source_id": str(item["source_id"])},
                devices=devices,
            )


def notify_budget_thresholds():
    """Categorías del período de presupuesto en curso (ver `workspace.
    budget_period`) que ya cruzaron el % de aviso del usuario."""
    today = timezone.localdate()

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not pref.warn_budget:
            continue
        devices = _devices_for(user)

        period_start = periods.period_start(today, workspace.budget_period)
        for row in budget_vs_actual(workspace, user, period_start)["rows"]:
            budgeted = row["budgeted"] + row["provision"]
            if budgeted <= 0:
                continue
            pct = (row["spent"] / budgeted) * 100
            if pct < pref.budget_threshold_pct:
                continue

            dedupe_key = f"{row['category']}:{period_start.isoformat()}"
            title = "Presupuesto superado" if pct >= 100 else "Presupuesto casi agotado"
            _notify(
                user, workspace, NotificationLog.KIND_BUDGET_THRESHOLD, dedupe_key,
                title=title,
                body=f"{row['category_name']}: {pct:.0f}% usado — {workspace.name}",
                data={
                    "type": NotificationLog.KIND_BUDGET_THRESHOLD,
                    "workspace": str(workspace.id),
                    "category": row["category"],
                },
                devices=devices,
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

        wallets = Wallet.objects.filter(
            workspace=workspace, is_archived=False, low_balance_threshold__isnull=False
        ).filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        for wallet in wallets:
            if wallet.current_balance >= wallet.low_balance_threshold:
                continue

            dedupe_key = f"{wallet.id}:{today.year}-{today.month:02d}"
            _notify(
                user, workspace, NotificationLog.KIND_LOW_BALANCE, dedupe_key,
                title="Saldo bajo",
                body=f"{wallet.name}: {_fmt_amount(wallet.current_balance)} {wallet.currency} — {workspace.name}",
                data={
                    "type": NotificationLog.KIND_LOW_BALANCE,
                    "workspace": str(workspace.id),
                    "wallet": str(wallet.id),
                },
                devices=devices,
            )


def notify_statement_due():
    """Tarjetas cuya fecha límite de pago (la de "pagar sin intereses") cae
    dentro de `statement_due_days_before` días y a las que todavía les falta
    pagar algo del último corte (ver `statement_payoff`) -- distinto de
    `notify_due_items`, que avisa cuota por cuota. Las compras hechas después
    del corte no entran: esas se cobran en el corte siguiente. Si ya se pagó
    el saldo al corte, no hay nada que avisar."""
    from apps.accounts.models import Wallet
    from apps.accounts.services import statement_payoff

    today = timezone.localdate()

    for membership in _active_memberships():
        user, workspace = membership.user, membership.workspace
        pref = _get_preference(user)
        if not pref.warn_statement_due:
            continue
        devices = _devices_for(user)

        wallets = (
            Wallet.objects.filter(
                workspace=workspace, kind=Wallet.KIND_CREDIT, is_archived=False
            )
            .exclude(billing_cycle_day__isnull=True)
            .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        )
        for wallet in wallets:
            payoff = statement_payoff(wallet, as_of=today)
            if payoff is None or payoff["remaining"] <= 0:
                continue
            due_date = payoff["payment_due_date"]
            days_left = (due_date - today).days
            if not (0 <= days_left <= pref.statement_due_days_before):
                continue

            when = "hoy" if days_left == 0 else f"antes del {due_date.strftime('%d/%m')}"
            dedupe_key = f"{wallet.id}:{due_date.isoformat()}"
            _notify(
                user, workspace, NotificationLog.KIND_STATEMENT_DUE, dedupe_key,
                title="Fecha límite de pago",
                body=(
                    f"{wallet.name}: pagá {_fmt_amount(payoff['remaining'])} {wallet.currency} "
                    f"{when} para no generar intereses (corte del "
                    f"{payoff['cutoff_date'].strftime('%d/%m')}) — {workspace.name}"
                ),
                data={
                    "type": NotificationLog.KIND_STATEMENT_DUE,
                    "workspace": str(workspace.id),
                    "wallet": str(wallet.id),
                    "amount": _fmt_amount(payoff["remaining"]),
                    "due_date": due_date.isoformat(),
                },
                devices=devices,
            )


def _monthly_summary_text(user, workspace, insights):
    """Gemini conecta los patrones en un solo texto (`apps.ai.summary.
    generate`); si no está disponible, se sigue mandando el texto armado a
    mano con los mismos datos -- la detección sigue siendo determinista, la
    IA sólo redacta (backlog, punto 6)."""
    from apps.ai.client import AIUnavailable
    from apps.ai.summary import generate as generate_summary

    try:
        result = generate_summary(user=user, workspace=workspace, insights=insights)
        return result["title"], result["body"]
    except AIUnavailable:
        body = " ".join(f"{i['title']}: {i['body']}" for i in insights)
        return "Tu resumen del mes", body[:500]


# Día del mes en que se dispara el resumen (para el mes que acaba de
# terminar, ver `notify_monthly_summary`) -- un día fijo y no una ventana
# móvil por membresía, mismo criterio que `INSIGHTS_WEEKDAY`.
MONTHLY_SUMMARY_DAY = 1


def notify_monthly_summary(today=None):
    """Un mensaje único que conecta los patrones de `behavior_insights` del
    mes que acaba de terminar, en vez de reavisarlos sueltos -- ésos siguen
    su propio camino semanal en `notify_insights`. Corre una vez al mes y no
    reemplaza al otro aviso: son dos toggles independientes a propósito
    (backlog, punto 6), para poder apagar uno sin el otro."""
    today = today or timezone.localdate()
    if today.day != MONTHLY_SUMMARY_DAY:
        return
    # Último día del mes que acaba de terminar: `behavior_insights` mira
    # hacia atrás desde `today`, así que correrlo con ayer da un resumen del
    # mes recién cerrado en vez de arrancar el que empieza hoy.
    reference_date = today - timezone.timedelta(days=1)
    dedupe_month = f"{reference_date.year}-{reference_date.month:02d}"

    prefs, devices_by_user = {}, {}

    for membership in _active_memberships().order_by("user_id"):
        user, workspace = membership.user, membership.workspace
        if user.pk not in prefs:
            prefs[user.pk] = _get_preference(user)
        pref = prefs[user.pk]
        if not pref.warn_monthly_summary:
            continue
        if user.pk not in devices_by_user:
            devices_by_user[user.pk] = _devices_for(user)
        devices = devices_by_user[user.pk]

        insights = behavior_insights(workspace, user, today=reference_date)
        if not insights:
            continue

        title, body = _monthly_summary_text(user, workspace, insights)
        _notify(
            user, workspace, NotificationLog.KIND_MONTHLY_SUMMARY, f"{workspace.id}:{dedupe_month}",
            title=title,
            body=body,
            data={"type": NotificationLog.KIND_MONTHLY_SUMMARY, "workspace": str(workspace.id)},
            devices=devices,
        )


def notify_insights(today=None):
    """Patrones de comportamiento de gasto (ver `apps.reports.services.
    behavior_insights`). La cadencia de aviso es semanal (o mensual) por
    `dedupe_key`, y `behavior_insights` cuesta ~12 queries por membresía, así
    que se calcula sólo un día de la semana en vez de todos: a diario, 6 de
    cada 7 cálculos terminaban descartados por el `dedupe_key`.

    El precio de esto es que si el job no corre ese día (Scheduler caído), la
    semana se salta. Es aceptable para un aviso de patrón; si dejara de serlo,
    el reemplazo es un marcador semanal por membresía en vez del día fijo."""
    today = today or timezone.localdate()
    if today.weekday() != INSIGHTS_WEEKDAY:
        return

    # Una persona en varios workspaces tiene una sola preferencia y un solo
    # juego de dispositivos: se consultan una vez por usuario, no por membresía.
    prefs, devices_by_user = {}, {}

    for membership in _active_memberships().order_by("user_id"):
        user, workspace = membership.user, membership.workspace
        if user.pk not in prefs:
            prefs[user.pk] = _get_preference(user)
        pref = prefs[user.pk]
        if not pref.warn_insights:
            continue
        if user.pk not in devices_by_user:
            devices_by_user[user.pk] = _devices_for(user)
        devices = devices_by_user[user.pk]

        for insight in behavior_insights(workspace, user, today=today):
            _notify(
                user, workspace, NotificationLog.KIND_INSIGHT, insight["dedupe_key"],
                title=insight["title"],
                body=f"{insight['body']} — {workspace.name}",
                data={"type": NotificationLog.KIND_INSIGHT, "workspace": str(workspace.id)},
                devices=devices,
            )
