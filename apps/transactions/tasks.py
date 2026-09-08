from celery import shared_task

from . import services


@shared_task
def generate_recurring_transactions():
    """Diaria: materializa los gastos recurrentes vencidos."""
    return [str(t.id) for t in services.generate_recurring_transactions()]
