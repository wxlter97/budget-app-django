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

from django.utils import timezone

from apps.reports.services import budget_vs_actual, upcoming_scheduled
from apps.workspaces.models import Membership

from .models import NotificationLog, NotificationPreference, PushDevice

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# Límite documentado de Expo por request.
_BATCH_SIZE = 100


def send_push(devices, *, title, body, data=None):
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
