"""Transferencias entre cuentas + flag `counts_toward_budget`."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.reports.services import budget_vs_actual, monthly_cashflow
from apps.transactions.models import Category, CategoryBudget, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def money(x):
    return Decimal(x).quantize(Decimal("0.01"))


class TransferTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )
        cls.a = Account.objects.create(
            workspace=cls.ws, name="A", type=Account.TYPE_CHECKING,
            opening_balance=Decimal("100.00"),
        )
        cls.b = Account.objects.create(
            workspace=cls.ws, name="B", type=Account.TYPE_SAVINGS,
            opening_balance=Decimal("0.00"),
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _post(self, payload):
        return self.client.post(
            "/api/v1/transactions/", payload, format="json", **{HEADER: str(self.ws.id)}
        )

    def test_transfer_moves_balance_between_accounts(self):
        res = self._post(
            {
                "type": "transfer",
                "account": str(self.a.id),
                "to_account": str(self.b.id),
                "amount": "30.00",
                "date": "2026-01-10",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNone(res.data["category"])
        self.assertFalse(res.data["counts_toward_budget"])

        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.current_balance, money("70.00"))
        self.assertEqual(self.b.current_balance, money("30.00"))

    def test_transfer_requires_distinct_accounts(self):
        res = self._post(
            {
                "type": "transfer",
                "account": str(self.a.id),
                "to_account": str(self.a.id),
                "amount": "10.00",
                "date": "2026-01-10",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("to_account", res.data)

    def test_transfer_excluded_from_cashflow(self):
        Transaction.objects.create(
            type="transfer", account=self.a, to_account=self.b,
            amount=Decimal("30.00"), date=dt.date(2026, 1, 10),
        )
        series = monthly_cashflow(self.ws, self.user, months=1, until=dt.date(2026, 1, 15))
        self.assertEqual(series[0]["income"], money("0.00"))
        self.assertEqual(series[0]["expenses"], money("0.00"))

    def test_deleting_transfer_reverts_both_balances(self):
        t = Transaction.objects.create(
            type="transfer", account=self.a, to_account=self.b,
            amount=Decimal("40.00"), date=dt.date(2026, 1, 10),
        )
        t.delete()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.current_balance, money("100.00"))
        self.assertEqual(self.b.current_balance, money("0.00"))


class BudgetFlagTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("bob", "b@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )
        cls.acc = Account.objects.create(
            workspace=cls.ws, name="C", type=Account.TYPE_CHECKING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        CategoryBudget.objects.create(
            workspace=cls.ws, category=cls.food, month=1, year=2026, amount=Decimal("100.00")
        )

    def test_out_of_budget_expense_affects_balance_but_not_budget(self):
        Transaction.objects.create(
            account=self.acc, category=self.food, amount=Decimal("60.00"),
            date=dt.date(2026, 1, 5),
        )
        Transaction.objects.create(
            account=self.acc, category=self.food, amount=Decimal("500.00"),
            date=dt.date(2026, 1, 6), counts_toward_budget=False,
        )

        self.acc.refresh_from_db()
        self.assertEqual(self.acc.current_balance, money("-560.00"))  # ambas mueven el saldo

        report = budget_vs_actual(self.ws, self.user, 2026, 1)
        row = next(r for r in report["rows"] if r["category_name"] == "Comida")
        self.assertEqual(row["spent"], money("60.00"))  # la de 500 no cuenta
        self.assertEqual(row["remaining"], money("40.00"))
