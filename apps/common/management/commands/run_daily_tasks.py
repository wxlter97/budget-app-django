"""
Corre las tareas periódicas del día en el orden correcto, para el entorno
sin Celery de producción (Cloud Run Job + Cloud Scheduler, ver DEPLOY.md
§6) -- reemplaza el `manage.py shell -c "..."` de una sola línea que tenía
antes ese comando, frágil y sin feedback si algo falla a mitad de camino.

Mismo orden que documenta `CELERY_BEAT_SCHEDULE` en settings.py: recurrentes
-> cierre de mes -> recordatorios. Cada tarea ya es idempotente por su
cuenta (una corrida de más el mismo día no duplica nada), así que no pasa
nada si el Cloud Scheduler dispara esto más de una vez, o a una hora que no
es la ideal.

    python manage.py run_daily_tasks
"""
from django.core.management.base import BaseCommand

from apps.notifications.tasks import send_daily_reminders
from apps.reports.tasks import close_previous_month
from apps.transactions.tasks import generate_recurring_transactions


class Command(BaseCommand):
    help = "Corre recurrentes, cierre de mes y recordatorios diarios, en ese orden."

    def handle(self, *args, **options):
        steps = [
            ("Transacciones recurrentes", generate_recurring_transactions),
            ("Cierre de mes anterior", close_previous_month),
            ("Recordatorios diarios", send_daily_reminders),
        ]
        for label, task in steps:
            self.stdout.write(f"→ {label}...")
            # Llamar la tarea como función normal la corre sincrónica en
            # este mismo proceso (no hay broker acá) -- sólo `.delay()`/
            # `.apply_async()` necesitarían uno.
            task()
            self.stdout.write(self.style.SUCCESS(f"  listo: {label}"))

        self.stdout.write(self.style.SUCCESS("Listo: tareas del día completas."))
