"""Día de inicio de la semana del presupuesto semanal (`Workspace.week_start_day`)."""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.common import periods
from apps.workspaces.models import Membership, Workspace

User = get_user_model()


class WeekStartPeriodsTests(APITestCase):
    # 2026-10-07 es miércoles.
    d = date(2026, 10, 7)

    def test_default_is_monday(self):
        self.assertEqual(periods.period_start(self.d, periods.WEEKLY), date(2026, 10, 5))

    def test_sunday_start(self):
        self.assertEqual(periods.period_start(self.d, periods.WEEKLY, 6), date(2026, 10, 4))
        # El domingo mismo abre su propia semana.
        self.assertEqual(periods.period_start(date(2026, 10, 4), periods.WEEKLY, 6), date(2026, 10, 4))
        # El sábado todavía pertenece a la semana que arrancó el domingo anterior.
        self.assertEqual(periods.period_start(date(2026, 10, 10), periods.WEEKLY, 6), date(2026, 10, 4))

    def test_wednesday_start_on_a_wednesday(self):
        self.assertEqual(periods.period_start(self.d, periods.WEEKLY, 2), self.d)

    def test_previous_period_respects_week_start(self):
        self.assertEqual(
            periods.previous_period_start(date(2026, 10, 4), periods.WEEKLY, 6), date(2026, 9, 27)
        )

    def test_week_start_ignored_for_other_periods(self):
        self.assertEqual(periods.period_start(self.d, periods.MONTHLY, 3), date(2026, 10, 1))


class WeekStartApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.ws = Workspace.objects.create(name="Casa", budget_period=periods.WEEKLY)
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.user)

    def test_defaults_to_monday_and_is_exposed(self):
        res = self.client.get(f"/api/v1/workspaces/{self.ws.id}/")
        self.assertEqual(res.data["week_start_day"], 0)

    def test_owner_can_change_it(self):
        res = self.client.patch(f"/api/v1/workspaces/{self.ws.id}/", {"week_start_day": 6})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.week_start_day, 6)
        # Mover la grilla reinicia el cierre de provisión (igual que cambiar de cadencia).
        self.assertIsNotNone(self.ws.budget_period_closed_through)

    def test_rejects_out_of_range(self):
        res = self.client.patch(f"/api/v1/workspaces/{self.ws.id}/", {"week_start_day": 7})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
