"""POST /api/v1/workspaces/{id}/reset/ — reinicio de datos del workspace."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, CategoryBudget, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()


class WorkspaceResetTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.member = User.objects.create_user("member", "m@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)

        self.wallet = Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("1000.00"),
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE,
        )
        Transaction.objects.create(
            wallet=self.wallet, category=self.cat, amount=Decimal("40.00"),
            type=Transaction.TYPE_EXPENSE, date="2026-09-01", created_by=self.owner,
        )
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"), month=9, year=2026,
        )

    def _url(self):
        return f"/api/v1/workspaces/{self.ws.id}/reset/"

    def test_requires_confirm(self):
        self.client.force_authenticate(self.owner)
        res = self.client.post(self._url(), {"scope": "movimientos"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_member_cannot_reset(self):
        self.client.force_authenticate(self.member)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_reset_movimientos_keeps_wallets_and_categories(self):
        self.client.force_authenticate(self.owner)
        res = self.client.post(
            self._url(), {"scope": "movimientos", "confirm": True}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(CategoryBudget.objects.count(), 0)
        self.assertTrue(Wallet.objects.filter(id=self.wallet.id).exists())
        self.assertTrue(Category.objects.filter(id=self.cat.id).exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.current_balance, Decimal("1000.00"))

    def test_reset_todo_wipes_everything(self):
        self.client.force_authenticate(self.owner)
        res = self.client.post(
            self._url(), {"scope": "todo", "confirm": True}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(Wallet.objects.filter(workspace=self.ws).count(), 0)
        self.assertEqual(Category.objects.filter(workspace=self.ws).count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        # el workspace sigue existiendo
        self.assertTrue(Workspace.objects.filter(id=self.ws.id).exists())
