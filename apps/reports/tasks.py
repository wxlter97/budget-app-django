from celery import shared_task
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from . import services


@shared_task
def close_month(year, month):
    return [str(s.id) for s in services.close_month(year, month)]


@shared_task
def close_previous_month():
    """Cierra el mes anterior. Lo agenda Celery Beat el día 1."""
    prev = timezone.localdate().replace(day=1) - relativedelta(months=1)
    return close_month(prev.year, prev.month)
