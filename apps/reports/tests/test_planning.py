"""Gasto por miembro, "¿me alcanza?", aportes a metas y resumen semanal."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.notifications.models import Notification, NotificationPreference
from apps.notifications.services import notify_statement_cutoff, notify_weekly_summary
from apps.reports import planning
from apps.transactions.models import Category, CategoryBudget, Person, Transaction
from apps.common import periods
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class PlanningBase(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw", first_name="Alice")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw", first_name="Bob")
        self.ws = Workspace.objects.create(name="Casa")
        self.m_alice = Membership.objects.create(workspace=self.ws, user=self.alice, role="owner")
        self.m_bob = Membership.objects.create(workspace=self.ws, user=self.bob)
        self.acc = Wallet.objects.create(workspace=self.ws, name="Cuenta", purpose="spending")
        self.food = Category.objects.create(workspace=self.ws, name="Comida", type="expense")
        self.fun = Category.objects.create(workspace=self.ws, name="Salidas", type="expense")
        self.today = timezone.localdate()
        self.client.force_authenticate(self.alice)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def spend(self, amount, cat=None, by="alice", date=None, **kw):
        return Transaction.objects.create(
            wallet=self.acc, type="expense", category=cat or self.food,
            amount=Decimal(amount), date=date or self.today,
            created_by=self.alice if by == "alice" else by, **kw,
        )


class MemberSpendingTests(PlanningBase):
    def test_splits_by_creator_and_payer(self):
        self.spend("30", by=self.alice)
        self.spend("20", by=self.bob)
        # Alice la cargó pero la pagó Bob (división entre personas).
        bob_person = Person.objects.create(workspace=self.ws, name="Bob", member=self.m_bob)
        self.spend("50", by=self.alice, paid_by=bob_person)
        self.spend("5", by=None)

        resp = self.client.get("/api/v1/reports/members/")

        self.assertEqual(resp.status_code, 200, resp.data)
        rows = {r["name"]: r for r in resp.data["members"]}
        self.assertEqual(rows["Bob"]["spent"], "70.00")
        self.assertEqual(rows["Alice"]["spent"], "30.00")
        self.assertEqual(rows["Sin asignar"]["spent"], "5.00")
        self.assertEqual(resp.data["total"], "105.00")

    def test_rejects_bad_month(self):
        self.assertEqual(self.client.get("/api/v1/reports/members/?month=13").status_code, 400)


class CanAffordTests(PlanningBase):
    def setUp(self):
        super().setUp()
        start = periods.period_start(self.today, self.ws.budget_period)
        CategoryBudget.objects.create(workspace=self.ws, category=self.food, amount=Decimal("100"), period_start=start)
        CategoryBudget.objects.create(workspace=self.ws, category=self.fun, amount=Decimal("100"), period_start=start)
        self.spend("60", cat=self.food)

    def test_ok_when_it_fits(self):
        data = planning.can_afford(self.ws, self.alice, Decimal("10"), category=self.food)
        self.assertEqual(data["verdict"], "ok")
        self.assertEqual(data["category"]["remaining_after"], Decimal("30"))
        self.assertEqual(data["available_after"], Decimal("130"))

    def test_tight_when_category_almost_empty(self):
        data = planning.can_afford(self.ws, self.alice, Decimal("35"), category=self.food)
        self.assertEqual(data["verdict"], "tight")

    def test_over_when_category_goes_negative(self):
        data = planning.can_afford(self.ws, self.alice, Decimal("50"), category=self.food)
        self.assertEqual(data["verdict"], "over")

    def test_endpoint_validates_category_of_other_workspace(self):
        other = Workspace.objects.create(name="Otro")
        foreign = Category.objects.create(workspace=other, name="X", type="expense")
        resp = self.client.get(f"/api/v1/reports/can-afford/?amount=10&category={foreign.id}")
        self.assertEqual(resp.status_code, 400)

    def test_endpoint(self):
        resp = self.client.get(f"/api/v1/reports/can-afford/?amount=10&category={self.food.id}")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["verdict"], "ok")
        self.assertEqual(resp.data["basis"], "budget")

    def test_without_budgets_uses_cashflow(self):
        CategoryBudget.objects.all().delete()
        Transaction.objects.create(
            wallet=self.acc, type="income", amount=Decimal("500"), date=self.today, created_by=self.alice
        )
        data = planning.can_afford(self.ws, self.alice, Decimal("100"))
        self.assertEqual(data["basis"], "cashflow")
        self.assertEqual(data["available_before"], Decimal("440"))
        self.assertEqual(data["verdict"], "ok")


class GoalContributionsTests(PlanningBase):
    def test_contributions_per_member(self):
        goal = Wallet.objects.create(
            workspace=self.ws, name="Viaje", purpose="savings", goal_amount=Decimal("1000")
        )
        for amount, who in (("100", self.alice), ("300", self.bob)):
            Transaction.objects.create(
                wallet=self.acc, to_wallet=goal, type="transfer", amount=Decimal(amount),
                date=self.today, created_by=who,
            )
        Transaction.objects.create(
            wallet=goal, to_wallet=self.acc, type="transfer", amount=Decimal("50"),
            date=self.today, created_by=self.bob,
        )

        resp = self.client.get(f"/api/v1/wallets/{goal.id}/contributions/")

        self.assertEqual(resp.status_code, 200, resp.data)
        rows = {r["name"]: r for r in resp.data["members"]}
        self.assertEqual(rows["Bob"]["contributed"], "300.00")
        self.assertEqual(rows["Bob"]["net"], "250.00")
        self.assertEqual(rows["Alice"]["share_pct"], 25.0)

    def test_not_savings_is_404(self):
        self.assertEqual(self.client.get(f"/api/v1/wallets/{self.acc.id}/contributions/").status_code, 404)


class WeeklySummaryTests(PlanningBase):
    def monday(self):
        return self.today - dt.timedelta(days=self.today.weekday())

    def test_summary_of_last_week(self):
        monday = self.monday()
        self.spend("40", cat=self.food, date=monday - dt.timedelta(days=3))
        self.spend("10", cat=self.fun, date=monday - dt.timedelta(days=2))
        self.spend("25", cat=self.food, date=monday - dt.timedelta(days=9))

        data = planning.weekly_summary(self.ws, self.alice, today=monday)

        self.assertEqual(data["total"], Decimal("50"))
        self.assertEqual(data["previous_total"], Decimal("25"))
        self.assertEqual(data["top_category_name"], "Comida")
        self.assertAlmostEqual(data["change_pct"], 100.0)

    def test_notification_only_on_monday_and_once(self):
        monday = self.monday()
        self.spend("40", date=monday - dt.timedelta(days=3))

        notify_weekly_summary(today=monday + dt.timedelta(days=1))
        self.assertFalse(Notification.objects.filter(kind="weekly_summary").exists())

        notify_weekly_summary(today=monday)
        notify_weekly_summary(today=monday)
        self.assertEqual(Notification.objects.filter(kind="weekly_summary", user=self.alice).count(), 1)

    def test_respects_preference(self):
        NotificationPreference.objects.create(user=self.alice, warn_weekly_summary=False)
        monday = self.monday()
        self.spend("40", date=monday - dt.timedelta(days=3))
        notify_weekly_summary(today=monday)
        self.assertFalse(Notification.objects.filter(kind="weekly_summary", user=self.alice).exists())


class StatementCutoffTests(PlanningBase):
    def test_warns_two_days_before_cutoff(self):
        today = dt.date(2026, 9, 13)
        Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose="debt", kind="credit",
            billing_cycle_day=15, payment_due_day=5,
        )

        notify_statement_cutoff(today=today)
        notify_statement_cutoff(today=today + dt.timedelta(days=1))

        n = Notification.objects.filter(kind="statement_cutoff", user=self.alice)
        self.assertEqual(n.count(), 1)
        self.assertIn("Visa corta el 15/09", n.first().title)

    def test_no_warning_on_cutoff_day(self):
        Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose="debt", kind="credit", billing_cycle_day=15,
        )
        notify_statement_cutoff(today=dt.date(2026, 9, 15))
        self.assertFalse(Notification.objects.filter(kind="statement_cutoff").exists())
