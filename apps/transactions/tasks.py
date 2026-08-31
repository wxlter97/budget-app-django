from celery import shared_task

from . import services


@shared_task
def generate_due_recurring_expenses():
    return [str(t.id) for t in services.generate_due_recurring_expenses()]


@shared_task
def post_due_installments():
    return [str(t.id) for t in services.post_due_installments()]
