from celery import shared_task

from . import services


@shared_task
def generate_recurring_transactions():
    """Diaria: materializa los gastos recurrentes vencidos."""
    return [str(t.id) for t in services.generate_recurring_transactions()]


@shared_task
def post_due_installments():
    return [str(t.id) for t in services.post_due_installments()]
