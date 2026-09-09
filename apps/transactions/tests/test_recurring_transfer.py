"""Recurrentes de tipo transferencia (aporte automático a una cartera de
ahorro, p. ej.) + que `type` siga siendo opcional/deducido para income y
expense como antes."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, RecurringExpense
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class RecurringExpenseTypeApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.checking = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.savings = Wallet.objects.create(
            workspace=cls.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS
        )
        cls.expense_cat = Category.objects.create(
            workspace=cls.ws, name="Netflix", type=Category.TYPE_EXPENSE
        )
        cls.income_cat = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def _post(self, payload):
        return self.client.post("/api/v1/recurring-expenses/", payload, format="json")

    def test_type_is_deduced_from_category_when_omitted(self):
        """Compatibilidad: un cliente que sólo manda category/wallet (como
        antes de agregar `type`) sigue funcionando igual."""
        res = self._post(
            {
                "category": str(self.income_cat.id),
                "wallet": str(self.checking.id),
                "amount": "1200.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["type"], "income")

    def test_transfer_requires_to_wallet(self):
        res = self._post(
            {
                "type": "transfer",
                "wallet": str(self.checking.id),
                "amount": "50.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("to_wallet", res.data)

    def test_transfer_to_wallet_must_differ_from_origin(self):
        res = self._post(
            {
                "type": "transfer",
                "wallet": str(self.checking.id),
                "to_wallet": str(self.checking.id),
                "amount": "50.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("to_wallet", res.data)

    def test_creates_transfer_without_category(self):
        res = self._post(
            {
                "type": "transfer",
                "name": "Aporte a Ahorro",
                "wallet": str(self.checking.id),
                "to_wallet": str(self.savings.id),
                "amount": "100.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNone(res.data["category"])
        self.assertEqual(str(res.data["to_wallet"]), str(self.savings.id))

    def test_expense_requires_category(self):
        res = self._post(
            {
                "type": "expense",
                "wallet": str(self.checking.id),
                "amount": "10.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("category", res.data)

    def test_category_type_must_match_declared_type(self):
        res = self._post(
            {
                "type": "income",
                "category": str(self.expense_cat.id),
                "wallet": str(self.checking.id),
                "amount": "10.00",
                "frequency": "monthly",
                "next_due_date": "2026-10-01",
            }
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("category", res.data)

    def test_switching_an_existing_expense_to_transfer_clears_category(self):
        rec = RecurringExpense.objects.create(
            workspace=self.ws, category=self.expense_cat, wallet=self.checking,
            type=RecurringExpense.TYPE_EXPENSE, amount=Decimal("10.00"),
            next_due_date=dt.date(2026, 10, 1),
        )
        res = self.client.patch(
            f"/api/v1/recurring-expenses/{rec.id}/",
            {"type": "transfer", "to_wallet": str(self.savings.id)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        rec.refresh_from_db()
        self.assertIsNone(rec.category_id)
        self.assertEqual(rec.to_wallet_id, self.savings.id)
