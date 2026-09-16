"""Racha de días sin gasto fuera de presupuesto, fines de semana sin gastos,
% de ahorro mensual y badges (subset inicial de gamificación)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.gamification import services
from apps.gamification.models import Badge, WorkspaceBadge
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def _backdate_workspace(workspace, date):
    Workspace.objects.filter(pk=workspace.pk).update(
        created_at=timezone.make_aware(dt.datetime.combine(date, dt.time.min))
    )
    workspace.refresh_from_db()


class GamificationServiceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING)
        cls.expense_cat = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)
        cls.income_cat = Category.objects.create(workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME)

    def setUp(self):
        _backdate_workspace(self.ws, dt.date(2026, 1, 1))

    def _expense(self, date, amount="10.00", counts_toward_budget=True):
        return Transaction.objects.create(
            wallet=self.wallet, category=self.expense_cat, amount=Decimal(amount),
            date=date, type=Transaction.TYPE_EXPENSE, counts_toward_budget=counts_toward_budget,
        )

    def _income(self, date, amount):
        return Transaction.objects.create(
            wallet=self.wallet, category=self.income_cat, amount=Decimal(amount),
            date=date, type=Transaction.TYPE_INCOME,
        )

    def test_no_spend_day_true_without_expenses(self):
        self.assertTrue(services.is_no_spend_day(self.ws, dt.date(2026, 3, 1)))

    def test_no_spend_day_false_with_counted_expense(self):
        self._expense(dt.date(2026, 3, 1))
        self.assertFalse(services.is_no_spend_day(self.ws, dt.date(2026, 3, 1)))

    def test_no_spend_day_true_when_expense_excluded_from_budget(self):
        # p. ej. una transferencia a ahorro marcada fuera de presupuesto --
        # no debería romper la racha.
        self._expense(dt.date(2026, 3, 1), counts_toward_budget=False)
        self.assertTrue(services.is_no_spend_day(self.ws, dt.date(2026, 3, 1)))

    def test_current_streak_counts_backward_from_as_of(self):
        as_of = dt.date(2026, 3, 10)
        self._expense(dt.date(2026, 3, 7))  # racha se corta acá
        # 3/8, 3/9, 3/10 sin gasto -> racha de 3
        self.assertEqual(services.current_streak(self.ws, as_of), 3)

    def test_current_streak_stops_at_workspace_creation(self):
        _backdate_workspace(self.ws, dt.date(2026, 3, 8))
        as_of = dt.date(2026, 3, 10)
        # sin ningún gasto, pero el workspace no existía antes del 3/8
        self.assertEqual(services.current_streak(self.ws, as_of), 3)  # 8, 9, 10

    def test_longest_streak_finds_best_run_not_just_current(self):
        _backdate_workspace(self.ws, dt.date(2026, 3, 1))
        # racha larga 3/1-3/10 (gasto el 3/11), luego racha corta 3/12-3/13
        self._expense(dt.date(2026, 3, 11))
        self._expense(dt.date(2026, 3, 14))
        today = dt.date(2026, 3, 14)
        with self._freeze_today(today):
            self.assertEqual(services.longest_streak(self.ws), 10)  # 3/1 al 3/10

    def test_no_spend_weekends_count(self):
        _backdate_workspace(self.ws, dt.date(2026, 3, 1))
        # sáb 2026-03-07 / dom 2026-03-08: sin gastos
        # sáb 2026-03-14 / dom 2026-03-15: gasto el sábado, no cuenta
        self._expense(dt.date(2026, 3, 14))
        today = dt.date(2026, 3, 16)
        with self._freeze_today(today):
            self.assertEqual(services.no_spend_weekends_count(self.ws), 1)

    def test_monthly_savings_percentage_basic(self):
        self._income(dt.date(2026, 3, 1), "1000.00")
        self._expense(dt.date(2026, 3, 2), "800.00")
        pct = services.monthly_savings_percentage(self.ws, 2026, 3)
        self.assertEqual(pct, Decimal("20.0"))

    def test_monthly_savings_percentage_none_without_income(self):
        self._expense(dt.date(2026, 3, 2), "50.00")
        self.assertIsNone(services.monthly_savings_percentage(self.ws, 2026, 3))

    def test_evaluate_and_award_badges_is_idempotent(self):
        first = services.evaluate_and_award_badges(self.ws, longest=7, weekends=0, savings_pct=None)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].badge.code, services.BADGE_STREAK_7)
        second = services.evaluate_and_award_badges(self.ws, longest=7, weekends=0, savings_pct=None)
        self.assertEqual(second, [])
        self.assertEqual(WorkspaceBadge.objects.filter(workspace=self.ws).count(), 1)

    def test_evaluate_and_award_badges_multiple_thresholds_at_once(self):
        awarded = services.evaluate_and_award_badges(self.ws, longest=30, weekends=1, savings_pct=Decimal("15.0"))
        codes = {wb.badge.code for wb in awarded}
        self.assertEqual(
            codes,
            {
                services.BADGE_STREAK_7,
                services.BADGE_STREAK_30,
                services.BADGE_FIRST_NO_SPEND_WEEKEND,
                services.BADGE_SAVINGS_10PCT,
            },
        )

    def test_summary_shape_and_lazily_awards_badges(self):
        _backdate_workspace(self.ws, dt.date(2026, 1, 1))
        today = dt.date(2026, 3, 15)
        with self._freeze_today(today):
            data = services.summary(self.ws)
        self.assertEqual(data["current_streak"], data["longest_streak"])
        self.assertIsNone(data["monthly_savings_pct"])
        self.assertEqual(len(data["badges"]), len(services.BADGE_CATALOG))
        streak_badge = next(b for b in data["badges"] if b["code"] == services.BADGE_STREAK_30)
        self.assertTrue(streak_badge["earned"])
        self.assertEqual(WorkspaceBadge.objects.filter(workspace=self.ws).count(), 3)  # streak 7/30 + no_spend_weekend? no, sin fin de semana forzado

    class _freeze_today:
        """Congela `timezone.localdate()` tal como lo ven los servicios de
        gamificación, sin arrastrar una dependencia nueva (freezegun) solo
        para esto."""

        def __init__(self, day):
            self.day = day
            self._patcher = None

        def __enter__(self):
            from unittest.mock import patch

            self._patcher = patch("apps.gamification.services.timezone.localdate", return_value=self.day)
            self._patcher.start()
            return self

        def __exit__(self, *exc):
            self._patcher.stop()


class GamificationApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_summary_endpoint_shape(self):
        resp = self.client.get("/api/v1/gamification/summary/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("current_streak", resp.data)
        self.assertIn("badges", resp.data)
        self.assertEqual(len(resp.data["badges"]), Badge.objects.count())

    def test_summary_requires_workspace_header(self):
        self.client.credentials()
        resp = self.client.get("/api/v1/gamification/summary/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
