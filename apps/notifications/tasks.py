from celery import shared_task

from . import services


@shared_task
def send_daily_reminders():
    """Recurrentes/cuotas que vencen mañana + presupuestos por agotarse.
    Idempotente: `NotificationLog` evita reavisar lo mismo si corre dos veces."""
    services.notify_due_items()
    services.notify_budget_thresholds()
