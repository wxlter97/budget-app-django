import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Wallet
from apps.reports.models import MonthlySnapshot
from apps.reports.services import close_month, net_worth, net_worth_breakdown
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    Transaction,
)
from apps.workspaces.models import Workspace


class NetWorthTests(TestCase):
    def test_net_worth_sums_flagged_wallets_across_purposes(self):
        ws = Workspace.objects.create(name="W")
        Wallet.objects.create(
            workspace=ws, name="C", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("1000.00"),
        )
        Wallet.objects.create(
            workspace=ws, name="Auto", purpose=Wallet.PURPOSE_ASSET,
            opening_balance=Decimal("5000.00"),
        )
        Wallet.objects.create(
            workspace=ws, name="Prestamo", purpose=Wallet.PURPOSE_DEBT,
            opening_balance=Decimal("-2000.00"),
        )
        # una cartera que NO cuenta para el neto
        Wallet.objects.create(
            workspace=ws, name="Ahorro viaje", purpose=Wallet.PURPOSE_SAVINGS,
            opening_balance=Decimal("800.00"), counts_toward_net_worth=False,
        )
        # 1000 + 5000 - 2000 (los 800 de ahorro no cuentan)
        self.assertEqual(net_worth(ws), Decimal("4000.00"))

        breakdown = net_worth_breakdown(ws)
        self.assertEqual(breakdown["net"], Decimal("4000.00"))
        self.assertEqual(breakdown["by_purpose"]["savings"], Decimal("800.00"))
        self.assertEqual(breakdown["by_purpose"]["asset"], Decimal("5000.00"))


class CloseMonthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.account = Wallet.objects.create(
            workspace=cls.ws, name="C", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.salary = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def _txn(self, cat, amount, day=10):
        return Transaction.objects.create(
            wallet=self.account, category=cat, amount=Decimal(amount),
            date=dt.date(2026, 1, day),
        )

    def test_snapshot_totals(self):
        self._txn(self.salary, "2000.00")
        self._txn(self.food, "300.00")
        self._txn(self.food, "150.00", day=20)
        # ruido de otro mes
        Transaction.objects.create(
            wallet=self.account, category=self.food, amount=Decimal("99.00"),
            date=dt.date(2026, 2, 1),
        )

        (snap,) = close_month(2026, 1, workspace=self.ws)
        self.assertEqual(snap.total_income, Decimal("2000.00"))
        self.assertEqual(snap.total_expenses, Decimal("450.00"))

    def test_close_month_is_idempotent(self):
        self._txn(self.food, "100.00")
        close_month(2026, 1, workspace=self.ws)
        close_month(2026, 1, workspace=self.ws)
        self.assertEqual(
            MonthlySnapshot.objects.filter(workspace=self.ws, year=2026, month=1).count(), 1
        )

    def test_provision_rollover_adds_leftover(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.food, amount=Decimal("500.00"), month=1, year=2026
        )
        self._txn(self.food, "300.00")  # sobran 200

        close_month(2026, 1, workspace=self.ws)
        provision = CategoryProvision.objects.get(category=self.food)
        self.assertEqual(provision.accumulated_amount, Decimal("200.00"))

    def test_overspent_category_does_not_create_negative_provision(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.food, amount=Decimal("100.00"), month=1, year=2026
        )
        self._txn(self.food, "250.00")  # se pasó

        close_month(2026, 1, workspace=self.ws)
        self.assertFalse(CategoryProvision.objects.filter(category=self.food).exists())
