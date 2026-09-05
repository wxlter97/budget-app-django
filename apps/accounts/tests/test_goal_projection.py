"""Proyección de metas de ahorro y de deudas: "a este ritmo la alcanzás /
la saldás en N meses" (Fase 2 del roadmap; deudas: Capa 3)."""
import datetime as dt
import math
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import goal_projection
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"
_UNTIL = timezone.localdate().replace(day=1)


def months_ago(n):
    return _UNTIL - relativedelta(months=n)


def _backdate(wallet, date):
    # `created_at` es `auto_now_add`: sólo se puede pisar con un UPDATE
    # (un `.save()` normal lo ignoraría).
    Wallet.objects.filter(pk=wallet.pk).update(
        created_at=timezone.make_aware(dt.datetime.combine(date, dt.time.min))
    )
    wallet.refresh_from_db()


class GoalProjectionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.income_cat = Category.objects.create(
            workspace=cls.ws, name="Aporte", type=Category.TYPE_INCOME
        )

    def _savings_wallet(self, **kwargs):
        return Wallet.objects.create(
            workspace=self.ws, name="Viaje", purpose=Wallet.PURPOSE_SAVINGS, **kwargs
        )

    def test_none_for_non_savings_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING)
        self.assertIsNone(goal_projection(w))

    def test_none_when_savings_wallet_has_no_goal(self):
        w = self._savings_wallet()
        self.assertIsNone(goal_projection(w))

    def test_goal_already_reached(self):
        w = self._savings_wallet(goal_amount=Decimal("1000.00"), opening_balance=Decimal("1200.00"))
        data = goal_projection(w)
        self.assertEqual(data["months_to_goal"], 0)
        self.assertTrue(data["on_track"])
        self.assertIsNone(data["monthly_rate"])

    def test_projects_from_observed_contribution_rate(self):
        w = self._savings_wallet(goal_amount=Decimal("6000.00"))
        _backdate(w, months_ago(5))
        for n in (5, 4, 3):
            Transaction.objects.create(
                wallet=w, category=self.income_cat, amount=Decimal("500.00"),
                date=months_ago(n),
            )
        # meses -2, -1, 0: sin aportes -- el promedio los cuenta en 0.
        # `current_balance` lo actualizan los signals vía UPDATE en la BD
        # (F()), que no refleja en la instancia `w` que ya tenemos en Python.
        w.refresh_from_db()
        data = goal_projection(w, months=6)
        self.assertEqual(data["monthly_rate"], Decimal("250.00"))  # 1500 / 6 meses
        self.assertEqual(data["remaining"], Decimal("4500.00"))  # 6000 - 1500
        self.assertEqual(data["months_to_goal"], 18)  # ceil(4500/250)
        self.assertEqual(data["projected_date"], _UNTIL + relativedelta(months=18))

    def test_falls_back_to_planned_monthly_contribution_without_history(self):
        w = self._savings_wallet(
            goal_amount=Decimal("2000.00"), monthly_contribution=Decimal("200.00")
        )
        data = goal_projection(w)
        self.assertEqual(data["monthly_rate"], Decimal("200.00"))
        self.assertEqual(data["months_to_goal"], 10)  # ceil(2000/200)

    def test_no_rate_at_all_returns_no_projection(self):
        w = self._savings_wallet(goal_amount=Decimal("2000.00"))
        data = goal_projection(w)
        self.assertIsNone(data["months_to_goal"])
        self.assertFalse(data["on_track"])

    def test_on_track_false_when_projected_date_is_after_goal_date(self):
        w = self._savings_wallet(
            goal_amount=Decimal("2000.00"),
            monthly_contribution=Decimal("100.00"),
            goal_date=_UNTIL + relativedelta(months=5),
        )
        data = goal_projection(w)
        self.assertEqual(data["months_to_goal"], 20)
        self.assertFalse(data["on_track"])


class DebtProjectionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("bob", "bob@example.com", "pw")
        cls.ws = Workspace.objects.create(name="B")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def _debt_wallet(self, **kwargs):
        return Wallet.objects.create(
            workspace=self.ws, name="Préstamo", purpose=Wallet.PURPOSE_DEBT, **kwargs
        )

    def _source_wallet(self):
        return Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING
        )

    def _pay(self, source, debt, amount, date):
        Transaction.objects.create(
            wallet=source, to_wallet=debt, type=Transaction.TYPE_TRANSFER,
            amount=amount, date=date,
        )

    def test_none_for_debt_without_total(self):
        w = self._debt_wallet(opening_balance=Decimal("-500.00"))
        self.assertIsNone(goal_projection(w))

    def test_debt_already_paid_off(self):
        w = self._debt_wallet(goal_amount=Decimal("1000.00"), opening_balance=Decimal("0"))
        data = goal_projection(w)
        self.assertEqual(data["months_to_goal"], 0)
        self.assertTrue(data["on_track"])
        self.assertIsNone(data["monthly_rate"])

    def test_projects_from_observed_payment_rate(self):
        w = self._debt_wallet(goal_amount=Decimal("6000.00"), opening_balance=Decimal("-6000.00"))
        source = self._source_wallet()
        _backdate(w, months_ago(5))
        for n in (5, 4, 3):
            self._pay(source, w, Decimal("500.00"), months_ago(n))
        w.refresh_from_db()
        data = goal_projection(w, months=6)
        self.assertEqual(data["monthly_rate"], Decimal("250.00"))  # 1500 / 6 meses
        self.assertEqual(data["remaining"], Decimal("4500.00"))  # abs(-6000 + 1500)
        self.assertEqual(data["months_to_goal"], 18)  # ceil(4500/250), sin interés

    def test_no_rate_at_all_returns_no_projection(self):
        w = self._debt_wallet(goal_amount=Decimal("2000.00"), opening_balance=Decimal("-2000.00"))
        data = goal_projection(w)
        self.assertIsNone(data["months_to_goal"])
        self.assertFalse(data["on_track"])

    def test_interest_rate_lengthens_payoff_via_amortization(self):
        w = self._debt_wallet(
            goal_amount=Decimal("6000.00"),
            opening_balance=Decimal("-6000.00"),
            interest_rate=Decimal("24.00"),  # 24% anual = 2% mensual
        )
        source = self._source_wallet()
        _backdate(w, months_ago(0))
        self._pay(source, w, Decimal("500.00"), months_ago(0))
        w.refresh_from_db()
        data = goal_projection(w, months=1)
        simple_months = math.ceil(data["remaining"] / data["monthly_rate"])
        self.assertGreater(data["months_to_goal"], simple_months)

    def test_payment_below_interest_returns_no_projection(self):
        w = self._debt_wallet(
            goal_amount=Decimal("6000.00"),
            opening_balance=Decimal("-6000.00"),
            interest_rate=Decimal("60.00"),  # 60% anual = 5% mensual
        )
        source = self._source_wallet()
        _backdate(w, months_ago(0))
        self._pay(source, w, Decimal("100.00"), months_ago(0))  # no cubre ni el interés
        w.refresh_from_db()
        data = goal_projection(w, months=1)
        self.assertIsNone(data["months_to_goal"])
        self.assertFalse(data["on_track"])


class GoalProjectionApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_projection_endpoint_shape(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Viaje", purpose=Wallet.PURPOSE_SAVINGS,
            goal_amount=Decimal("2000.00"), monthly_contribution=Decimal("100.00"),
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/projection/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["months_to_goal"], 20)
        self.assertEqual(resp.data["monthly_rate"], "100.00")

    def test_projection_404_for_non_savings_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING)
        resp = self.client.get(f"/api/v1/wallets/{w.id}/projection/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_projection_endpoint_shape_for_debt(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Préstamo", purpose=Wallet.PURPOSE_DEBT,
            goal_amount=Decimal("2000.00"), opening_balance=Decimal("-2000.00"),
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/projection/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["remaining"], "2000.00")
        self.assertIsNone(resp.data["months_to_goal"])

    def test_projection_404_for_debt_without_total(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Préstamo", purpose=Wallet.PURPOSE_DEBT,
            opening_balance=Decimal("-500.00"),
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/projection/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
