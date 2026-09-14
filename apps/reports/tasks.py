from celery import shared_task
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from . import services


@shared_task
def close_month(year, month):
    return [str(s.id) for s in services.close_month(year, month)]


@shared_task
def close_previous_month():
    """Cierra el mes anterior (snapshot de patrimonio neto). Lo agenda
    Celery Beat el día 1."""
    prev = timezone.localdate().replace(day=1) - relativedelta(months=1)
    return close_month(prev.year, prev.month)


@shared_task
def close_previous_budget_period():
    """Rollover de provisión del último período de presupuesto ya cerrado de
    cada workspace, según su `budget_period` (ver `services.
    close_previous_budget_period` -- a diferencia de `close_previous_month`
    corre bien todos los días, no sólo el día 1, porque es idempotente por
    su cuenta)."""
    return services.close_previous_budget_period()
