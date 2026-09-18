"""Marcar una transacción como reembolsable / reembolsada."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class RefundableFlagsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-05-01", type=Transaction.TYPE_EXPENSE,
        )

    def test_default_flags_are_false(self):
        resp = self.client.get(f"/api/v1/transactions/{self.txn.id}/")
        self.assertFalse(resp.data["is_refundable"])
        self.assertFalse(resp.data["is_refunded"])

    def test_can_mark_as_refundable(self):
        resp = self.client.patch(
            f"/api/v1/transactions/{self.txn.id}/", {"is_refundable": True}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["is_refundable"])
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.is_refundable)

    def test_is_refunded_is_read_only_now(self):
        # HALLAZGO: antes se podía prender este flag a mano sin que se
        # moviera un centavo. Ahora sólo lo pone `register_refund` -- un
        # PATCH directo se ignora en silencio (DRF, campo read-only), no
        # tira error, pero tampoco cambia nada.
        self.txn.is_refundable = True
        self.txn.save(update_fields=["is_refundable"])
        resp = self.client.patch(
            f"/api/v1/transactions/{self.txn.id}/", {"is_refunded": True}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(resp.data["is_refunded"])
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.is_refunded)

    def test_filter_by_is_refundable(self):
        self.txn.is_refundable = True
        self.txn.save(update_fields=["is_refundable"])
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("10.00"),
            date="2026-05-02", type=Transaction.TYPE_EXPENSE,
        )
        resp = self.client.get("/api/v1/transactions/?is_refundable=true")
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(resp.data["results"][0]["id"], str(self.txn.id))
