"""`manage.py run_daily_tasks` -- ver DEPLOY.md §6 (Cloud Run Job sin
Celery). No verifica el resultado de negocio de cada tarea (eso ya lo
cubren los tests de cada app) -- sólo que el comando corre las tres sin
reventar, en cualquier entorno (incluso uno recién migrado, sin datos)."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class RunDailyTasksCommandTests(TestCase):
    def test_runs_without_crashing_on_an_empty_database(self):
        call_command("run_daily_tasks")


class RunDailyTasksFailureTests(TestCase):
    """Un paso que revienta no corta el resto, avisa a Discord y deja el
    comando en error para que Cloud Run marque la ejecución como fallida."""

    CMD = "apps.common.management.commands.run_daily_tasks"

    def _boom(self):
        raise RuntimeError("column foo does not exist")

    @override_settings(OPS_WEBHOOK_URL="https://discord.com/api/webhooks/x/y")
    def test_failure_keeps_going_notifies_and_raises(self):
        with (
            patch(f"{self.CMD}.send_daily_reminders", side_effect=self._boom),
            patch(f"{self.CMD}.backup_database") as backup,
            patch(f"{self.CMD}.requests.post") as post,
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_tasks", stdout=StringIO(), stderr=StringIO())

        backup.assert_called_once()  # el backup corrió igual
        post.assert_called_once()
        content = post.call_args.kwargs["json"]["content"]
        self.assertIn("Recordatorios diarios", content)
        self.assertIn("column foo does not exist", content)

    @override_settings(OPS_WEBHOOK_URL="https://discord.com/api/webhooks/x/y")
    def test_recurring_failure_skips_closings(self):
        with (
            patch(f"{self.CMD}.generate_recurring_transactions", side_effect=self._boom),
            patch(f"{self.CMD}.close_previous_month") as close_month,
            patch(f"{self.CMD}.close_previous_budget_period") as close_period,
            patch(f"{self.CMD}.send_daily_reminders") as reminders,
            patch(f"{self.CMD}.requests.post") as post,
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_tasks", stdout=StringIO(), stderr=StringIO())

        close_month.assert_not_called()
        close_period.assert_not_called()
        reminders.assert_called_once()
        self.assertIn("Sin correr por eso", post.call_args.kwargs["json"]["content"])

    @override_settings(OPS_WEBHOOK_URL="https://discord.com/api/webhooks/x/y")
    def test_success_sends_nothing(self):
        with patch(f"{self.CMD}.requests.post") as post:
            call_command("run_daily_tasks", stdout=StringIO())
        post.assert_not_called()

    @override_settings(OPS_WEBHOOK_URL="https://discord.com/api/webhooks/x/y")
    def test_discord_down_does_not_hide_the_failure(self):
        import requests

        with (
            patch(f"{self.CMD}.send_daily_reminders", side_effect=self._boom),
            patch(f"{self.CMD}.requests.post", side_effect=requests.ConnectionError),
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_tasks", stdout=StringIO(), stderr=StringIO())
