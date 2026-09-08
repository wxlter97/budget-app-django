"""Una cartera con hijas es puramente un contenedor (su saldo mostrado es la
suma de sus hijas, ver `Wallet.aggregated_balance`) -- no se le puede cargar
nada directamente, igual que a un grupo de categoría."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class WalletGroupValidationTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.owner)

        self.parent = Wallet.objects.create(workspace=self.ws, name="Multimoney", purpose=Wallet.PURPOSE_SAVINGS)
        self.child = Wallet.objects.create(
            workspace=self.ws, name="Fondo", purpose=Wallet.PURPOSE_SAVINGS, parent=self.parent
        )
        self.leaf = Wallet.objects.create(workspace=self.ws, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING)
        self.food = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)

    def _post(self, path, payload):
        return self.client.post(path, payload, format="json", **{HEADER: str(self.ws.id)})

    def test_rejects_transaction_with_group_wallet_as_source(self):
        resp = self._post("/api/v1/transactions/", {
            "type": "expense", "wallet": str(self.parent.id), "category": str(self.food.id),
            "amount": "10.00", "date": "2026-01-01",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_transaction_with_group_wallet_as_destination(self):
        resp = self._post("/api/v1/transactions/", {
            "type": "transfer", "wallet": str(self.leaf.id), "to_wallet": str(self.parent.id),
            "amount": "10.00", "date": "2026-01-01",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accepts_transaction_against_the_leaf_child(self):
        resp = self._post("/api/v1/transactions/", {
            "type": "expense", "wallet": str(self.child.id), "category": str(self.food.id),
            "amount": "10.00", "date": "2026-01-01",
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_rejects_recurring_expense_on_a_group_wallet(self):
        resp = self._post("/api/v1/recurring-expenses/", {
            "category": str(self.food.id), "wallet": str(self.parent.id),
            "amount": "10.00", "next_due_date": "2026-02-01",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_installment_purchase_on_a_group_wallet(self):
        resp = self._post("/api/v1/installment-purchases/", {
            "wallet": str(self.parent.id), "category": str(self.food.id),
            "description": "Compra", "total_amount": "300.00",
            "installments_total": 3, "start_date": "2026-01-01",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
