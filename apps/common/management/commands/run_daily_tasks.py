"""
Corre las tareas periódicas del día en el orden correcto, para el entorno
sin Celery de producción (Cloud Run Job + Cloud Scheduler, ver DEPLOY.md
§6) -- reemplaza el `manage.py shell -c "..."` de una sola línea que tenía
antes ese comando, frágil y sin feedback si algo falla a mitad de camino.

Mismo orden que documenta `CELERY_BEAT_SCHEDULE` en settings.py: recurrentes
-> cierres (mes y período de presupuesto) -> recordatorios. Cada tarea ya es
idempotente por su cuenta (una corrida de más el mismo día no duplica nada
-- el cierre de período de presupuesto en particular lleva su propio
`Workspace.budget_period_closed_through` para no duplicar la provisión
acumulada), así que no pasa nada si el Cloud Scheduler dispara esto más de
una vez, o a una hora que no es la ideal.

Al final corre `backup_database`, que es el único paso que no es idempotente en
el sentido estricto: cada corrida deja un volcado más en el bucket. No molesta
(los caduca la retención), pero es la razón de que vaya último.

Si un paso revienta, los demás igual corren (salvo los cierres, que dependen
de que los recurrentes hayan quedado al día), al final se avisa por Discord
(`OPS_WEBHOOK_URL`) qué pasos fallaron y el comando sale con error para que
Cloud Run marque la ejecución como fallida. Antes, el primer error cortaba
todo: si fallaban los recurrentes tampoco salían los recordatorios ni el
backup, y nadie se enteraba.

    python manage.py run_daily_tasks
"""
import logging
import os
import traceback

import requests
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.billing.services import send_renewal_reminders
from apps.notifications.tasks import send_daily_reminders
from apps.reports.tasks import close_previous_budget_period, close_previous_month
from apps.transactions.tasks import generate_recurring_transactions

logger = logging.getLogger(__name__)

RECURRING = "Transacciones recurrentes"
# Los cierres congelan el mes/período anterior: con los recurrentes a medio
# generar, congelarían números incompletos. Mejor no cerrar y que el cierre
# de mañana (idempotente) lo haga con todo en su lugar.
NEEDS_RECURRING = {"Cierre de mes anterior", "Cierre del período de presupuesto anterior"}

# Discord corta el mensaje en 2000 caracteres.
MAX_DISCORD_CONTENT = 1900


def backup_database():
    call_command("backup_database")


def notify_failures(failures, skipped):
    """Avisa a Discord qué pasos fallaron. Nunca revienta: el aviso es un
    extra, el error de verdad ya quedó en los logs y en Sentry."""
    webhook_url = settings.OPS_WEBHOOK_URL
    if not webhook_url:
        return

    # Cloud Run numera los intentos de una misma ejecución desde 0.
    attempt = int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "0")) + 1
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "local")
    lines = [f"🚨 **Job diario con errores** — `{execution}` (intento {attempt})"]
    for label, exc in failures:
        lines.append(f"• **{label}**: `{type(exc).__name__}: {str(exc)[:300]}`")
    if skipped:
        lines.append(f"• Sin correr por eso: {', '.join(skipped)}")
    lines.append("Logs: RUNBOOK.md §4.")
    content = "\n".join(lines)[:MAX_DISCORD_CONTENT]

    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except requests.RequestException:
        logger.warning("No se pudo avisar a Discord del job diario fallido.", exc_info=True)


class Command(BaseCommand):
    help = "Corre recurrentes, cierres y recordatorios diarios, en ese orden."

    def handle(self, *args, **options):
        steps = [
            (RECURRING, generate_recurring_transactions),
            ("Cierre de mes anterior", close_previous_month),
            ("Cierre del período de presupuesto anterior", close_previous_budget_period),
            ("Recordatorios diarios", send_daily_reminders),
            ("Vencimientos de suscripción", send_renewal_reminders),
            # El backup va al final, después de lo que el usuario nota: si
            # pg_dump falla o el bucket rechaza la subida, para entonces los
            # recurrentes y los recordatorios ya salieron.
            ("Backup de la base", backup_database),
        ]
        failures = []
        skipped = []
        for label, task in steps:
            if label in NEEDS_RECURRING and any(f[0] == RECURRING for f in failures):
                skipped.append(label)
                self.stdout.write(self.style.WARNING(f"→ {label}: se omite (fallaron los recurrentes)"))
                continue
            self.stdout.write(f"→ {label}...")
            try:
                # Llamar la tarea como función normal la corre sincrónica en
                # este mismo proceso (no hay broker acá) -- sólo `.delay()`/
                # `.apply_async()` necesitarían uno.
                task()
            except Exception as exc:  # noqa: BLE001 -- se reporta y se sigue
                # `logger.exception` también llega a Sentry (LoggingIntegration).
                logger.exception("Falló el paso del job diario: %s", label)
                self.stderr.write(traceback.format_exc())
                failures.append((label, exc))
                continue
            self.stdout.write(self.style.SUCCESS(f"  listo: {label}"))

        if failures:
            notify_failures(failures, skipped)
            names = ", ".join(label for label, _ in failures)
            raise CommandError(f"Tareas del día con errores: {names}")

        self.stdout.write(self.style.SUCCESS("Listo: tareas del día completas."))
