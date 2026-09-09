from celery import shared_task

from . import services


@shared_task
def send_daily_reminders():
    """Recurrentes/cuotas que vencen mañana + presupuestos por agotarse +
    saldo bajo + estado de cuenta por vencer. Idempotente: `NotificationLog`
    evita reavisar lo mismo si corre dos veces."""
    services.notify_due_items()
    services.notify_budget_thresholds()
    services.notify_low_balance()
    services.notify_statement_due()
