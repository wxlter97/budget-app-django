import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Account, Asset, Debt, Liability
from apps.reports.models import MonthlySnapshot
from apps.reports.services import close_month, net_worth
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    Transaction,
)
from apps.workspaces.models import Workspace


class NetWorthTests(TestCase):
    def test_net_worth_combines_accounts_assets_liabilities_debts(self):
        ws = Workspace.objects.create(name="W")
        Account.objects.create(
            workspace=ws, name="C", type=Account.TYPE_CHECKING,
            opening_balance=Decimal("1000.00"),
        )
        Asset.objects.create(workspace=ws, name="Auto", type="v", current_value=Decimal("5000.00"))
        Liability.objects.create(
            workspace=ws, name="Prestamo", type="p",
            total_amount=Decimal("3000.00"), remaining_amount=Decimal("2000.00"),
        )
        Debt.objects.create(
            workspace=ws, direction=Debt.DIRECTION_FAVOR, person="A", amount=Decimal("300.00")
        )
        Debt.objects.create(
            workspace=ws, direction=Debt.DIRECTION_CONTRA, person="B", amount=Decimal("100.00")
        )
        # 1000 + 5000 - 2000 + 300 - 100
        self.assertEqual(net_worth(ws), Decimal("4200.00"))


class CloseMonthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.account = Account.objects.create(
            workspace=cls.ws, name="C", type=Account.TYPE_CHECKING
        )
        cls.salary = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def _txn(self, cat, amount, day=10):
        return Transaction.objects.create(
            account=self.account, category=cat, amount=Decimal(amount),
            date=dt.date(2026, 1, day),
        )

    def test_snapshot_totals(self):
        self._txn(self.salary, "2000.00")
        self._txn(self.food, "300.00")
        self._txn(self.food, "150.00", day=20)
        # ruido de otro mes
        Transaction.objects.create(
            account=self.account, category=self.food, amount=Decimal("99.00"),
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
