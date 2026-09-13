"""`manage.py run_daily_tasks` -- ver DEPLOY.md §6 (Cloud Run Job sin
Celery). No verifica el resultado de negocio de cada tarea (eso ya lo
cubren los tests de cada app) -- sólo que el comando corre las tres sin
reventar, en cualquier entorno (incluso uno recién migrado, sin datos)."""
from django.core.management import call_command
from django.test import TestCase


class RunDailyTasksCommandTests(TestCase):
    def test_runs_without_crashing_on_an_empty_database(self):
        call_command("run_daily_tasks")
