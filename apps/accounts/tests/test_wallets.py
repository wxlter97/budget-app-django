"""Sub-carteras, saldo agregado, flag de patrimonio neto."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import recompute_wallet_balance
from apps.transactions.models import Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def money(x):
    return Decimal(x).quantize(Decimal("0.01"))


class WalletTreeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _post(self, payload):
        return self.client.post(
            "/api/v1/wallets/", payload, format="json", **{HEADER: str(self.ws.id)}
        )

    def test_aggregated_balance_includes_children(self):
        parent = Wallet.objects.create(
            workspace=self.ws, name="Multimoney", purpose="spending",
            opening_balance=Decimal("1000.00"),
        )
        child = Wallet.objects.create(
            workspace=self.ws, name="Fondo emergencias", purpose="savings",
            parent=parent, opening_balance=Decimal("4000.00"),
        )
        grandchild = Wallet.objects.create(
            workspace=self.ws, name="Sub-fondo", purpose="savings",
            parent=child, opening_balance=Decimal("500.00"),
        )
        parent.refresh_from_db()
        self.assertEqual(parent.current_balance, money("1000.00"))
        self.assertEqual(parent.aggregated_balance, money("5500.00"))
        self.assertEqual(child.aggregated_balance, money("4500.00"))
        self.assertEqual(grandchild.aggregated_balance, money("500.00"))

    def test_cannot_delete_wallet_with_children(self):
        parent = Wallet.objects.create(workspace=self.ws, name="P", purpose="spending")
        Wallet.objects.create(workspace=self.ws, name="H", purpose="spending", parent=parent)
        with self.assertRaises(ProtectedError):
            parent.delete()

    def test_api_rejects_parent_cycle(self):
        a = Wallet.objects.create(workspace=self.ws, name="A", purpose="spending")
        b = Wallet.objects.create(workspace=self.ws, name="B", purpose="spending", parent=a)

        res = self.client.patch(
            f"/api/v1/wallets/{a.id}/",
            {"parent": str(b.id)},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", res.data)

    def test_internal_transfer_parent_child_keeps_net_worth(self):
        from apps.reports.services import net_worth

        parent = Wallet.objects.create(
            workspace=self.ws, name="P", purpose="spending",
            opening_balance=Decimal("1000.00"),
        )
        child = Wallet.objects.create(
            workspace=self.ws, name="H", purpose="savings", parent=parent,
            opening_balance=Decimal("0.00"),
        )
        before = net_worth(self.ws)
        Transaction.objects.create(
            type="transfer", wallet=parent, to_wallet=child,
            amount=Decimal("300.00"), date="2026-01-10",
        )
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(parent.current_balance, money("700.00"))
        self.assertEqual(child.current_balance, money("300.00"))
        self.assertEqual(net_worth(self.ws), before)  # el neto no cambia

    def test_editing_opening_balance_recomputes_current(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Auto", purpose="asset",
            opening_balance=Decimal("8000.00"),
        )
        res = self.client.patch(
            f"/api/v1/wallets/{w.id}/",
            {"opening_balance": "7500.00"},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        w.refresh_from_db()
        self.assertEqual(w.current_balance, money("7500.00"))

    def test_flag_excludes_from_net_but_not_from_by_purpose(self):
        from apps.reports.services import net_worth_breakdown

        Wallet.objects.create(
            workspace=self.ws, name="Gasto", purpose="spending",
            opening_balance=Decimal("200.00"),
        )
        Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose="savings",
            opening_balance=Decimal("33000.00"), counts_toward_net_worth=False,
        )
        b = net_worth_breakdown(self.ws)
        self.assertEqual(b["net"], money("200.00"))
        self.assertEqual(b["by_purpose"]["savings"], money("33000.00"))
