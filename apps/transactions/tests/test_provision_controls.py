"""Provisión acumulada: interruptor global del workspace + "poner en cero"."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.reports.services import budget_vs_actual, close_previous_budget_period
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
)
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class ProvisionControlsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.food = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)
        self.fun = Category.objects.create(workspace=self.ws, name="Ocio", type=Category.TYPE_EXPENSE)
        self.client.force_authenticate(self.user)

    def _provision(self, cat, amount):
        return CategoryProvision.objects.create(category=cat, accumulated_amount=Decimal(amount))

    def _row(self, cat):
        report = budget_vs_actual(self.ws, self.user, dt.date(2026, 2, 1))
        return next(r for r in report["rows"] if r["category"] == str(cat.id))

    def test_global_switch_off_stops_accumulating(self):
        self.ws.rollover_surplus = False
        self.ws.save()
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.food, amount=Decimal("500"), period_start=dt.date(2026, 1, 1)
        )
        close_previous_budget_period(workspace=self.ws, as_of=dt.date(2026, 2, 1))
        self.assertFalse(CategoryProvision.objects.filter(category=self.food).exists())

    def test_global_switch_off_hides_accumulated_but_keeps_it(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.food, amount=Decimal("500"), period_start=dt.date(2026, 2, 1)
        )
        self._provision(self.food, "200")
        self.assertEqual(self._row(self.food)["provision"], Decimal("200"))

        self.ws.rollover_surplus = False
        self.ws.save()
        self.assertEqual(self._row(self.food)["provision"], Decimal("0"))
        self.assertEqual(CategoryProvision.objects.get(category=self.food).accumulated_amount, Decimal("200"))

    def test_owner_can_toggle_global_switch_via_api(self):
        res = self.client.patch(f"/api/v1/workspaces/{self.ws.id}/", {"rollover_surplus": False})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["rollover_surplus"])
        self.ws.refresh_from_db()
        self.assertFalse(self.ws.rollover_surplus)

    def test_reset_category_provision_only_touches_that_category(self):
        self._provision(self.food, "200")
        self._provision(self.fun, "50")
        res = self.client.post(f"/api/v1/categories/{self.food.id}/reset-provision/", **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(CategoryProvision.objects.get(category=self.food).accumulated_amount, 0)
        self.assertEqual(CategoryProvision.objects.get(category=self.fun).accumulated_amount, Decimal("50"))

    def test_reset_category_provision_without_provision_is_ok(self):
        res = self.client.post(f"/api/v1/categories/{self.food.id}/reset-provision/", **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_reset_all_provisions_is_owner_only_and_scoped(self):
        self._provision(self.food, "200")
        self._provision(self.fun, "50")
        other = Workspace.objects.create(name="Otro")
        foreign = Category.objects.create(workspace=other, name="X", type=Category.TYPE_EXPENSE)
        self._provision(foreign, "99")

        member = User.objects.create_user("m", "m@e.com", "pw")
        Membership.objects.create(workspace=self.ws, user=member, role=Membership.ROLE_MEMBER)
        self.client.force_authenticate(member)
        res = self.client.post(f"/api/v1/workspaces/{self.ws.id}/reset-provisions/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.user)
        res = self.client.post(f"/api/v1/workspaces/{self.ws.id}/reset-provisions/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["reset"], 2)
        self.assertEqual(CategoryProvision.objects.get(category=self.food).accumulated_amount, 0)
        self.assertEqual(CategoryProvision.objects.get(category=self.fun).accumulated_amount, 0)
        self.assertEqual(CategoryProvision.objects.get(category=foreign).accumulated_amount, Decimal("99"))
