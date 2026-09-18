"""Registrar el reembolso real de un gasto: `services.register_refund` +
`POST /transactions/{id}/register-refund/`."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.transactions.services import get_or_create_refund_category, register_refund
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class RegisterRefundServiceTests(APITestCase):
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
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-05-01", type=Transaction.TYPE_EXPENSE, description="Súper",
        )

    def test_creates_an_income_transaction_and_marks_refunded(self):
        refund = register_refund(
            original=self.txn, amount=Decimal("50.00"), date="2026-05-10",
            wallet=self.wallet, created_by=self.user,
        )

        self.assertEqual(refund.type, Transaction.TYPE_INCOME)
        self.assertEqual(refund.amount, Decimal("50.00"))
        self.assertEqual(refund.wallet, self.wallet)
        self.assertEqual(refund.refund_of, self.txn)
        self.assertEqual(refund.source, Transaction.SOURCE_REFUND)
        self.assertEqual(refund.category.name, "Reembolsos")
        self.assertEqual(refund.category.type, Category.TYPE_INCOME)

        self.txn.refresh_from_db()
        self.assertTrue(self.txn.is_refunded)

    def test_partial_refund_is_allowed(self):
        refund = register_refund(
            original=self.txn, amount=Decimal("20.00"), date="2026-05-10",
            wallet=self.wallet, created_by=self.user,
        )
        self.assertEqual(refund.amount, Decimal("20.00"))

    def test_amount_cannot_exceed_the_original(self):
        with self.assertRaises(ValidationError):
            register_refund(
                original=self.txn, amount=Decimal("999.00"), date="2026-05-10",
                wallet=self.wallet, created_by=self.user,
            )

    def test_amount_must_be_positive(self):
        with self.assertRaises(ValidationError):
            register_refund(
                original=self.txn, amount=Decimal("0"), date="2026-05-10",
                wallet=self.wallet, created_by=self.user,
            )

    def test_cannot_refund_an_income(self):
        income_cat = Category.objects.create(
            workspace=self.ws, name="Sueldo", type=Category.TYPE_INCOME
        )
        income = Transaction.objects.create(
            wallet=self.wallet, category=income_cat, amount=Decimal("100.00"),
            date="2026-05-01", type=Transaction.TYPE_INCOME,
        )
        with self.assertRaises(ValidationError):
            register_refund(
                original=income, amount=Decimal("100.00"), date="2026-05-10",
                wallet=self.wallet, created_by=self.user,
            )

    def test_cannot_refund_twice(self):
        register_refund(
            original=self.txn, amount=Decimal("50.00"), date="2026-05-10",
            wallet=self.wallet, created_by=self.user,
        )
        with self.assertRaises(ValidationError):
            register_refund(
                original=self.txn, amount=Decimal("10.00"), date="2026-05-11",
                wallet=self.wallet, created_by=self.user,
            )

    def test_reuses_an_existing_reembolsos_category_instead_of_duplicating(self):
        existing = Category.objects.create(
            workspace=self.ws, name="reembolsos", type=Category.TYPE_INCOME,
        )
        category = get_or_create_refund_category(self.ws)
        self.assertEqual(category, existing)
        self.assertEqual(
            Category.objects.filter(workspace=self.ws, name__iexact="Reembolsos").count(), 1
        )


class RegisterRefundApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.other_wallet = Wallet.objects.create(
            workspace=cls.ws, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING
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

    def _url(self, txn=None):
        return f"/api/v1/transactions/{(txn or self.txn).id}/register-refund/"

    def test_registers_a_refund_in_the_same_wallet_by_default(self):
        resp = self.client.post(self._url(), {"amount": "50.00", "date": "2026-05-10"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["type"], "income")
        self.assertEqual(resp.data["refund_of"], self.txn.id)

        original = self.client.get(f"/api/v1/transactions/{self.txn.id}/").data
        self.assertTrue(original["is_refunded"])
        self.assertEqual(original["refund_transaction_id"], str(resp.data["id"]))

    def test_can_credit_a_different_wallet(self):
        resp = self.client.post(
            self._url(), {"amount": "50.00", "date": "2026-05-10", "wallet": str(self.other_wallet.id)}
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["wallet"], self.other_wallet.id)

    def test_wallet_from_another_workspace_is_rejected(self):
        other_ws = Workspace.objects.create(name="Otro")
        foreign_wallet = Wallet.objects.create(
            workspace=other_ws, name="Ajena", purpose=Wallet.PURPOSE_SPENDING
        )
        resp = self.client.post(
            self._url(), {"amount": "50.00", "date": "2026-05-10", "wallet": str(foreign_wallet.id)}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_amount_over_original_is_rejected(self):
        resp = self.client.post(self._url(), {"amount": "500.00", "date": "2026-05-10"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_second_refund_attempt_is_rejected(self):
        self.client.post(self._url(), {"amount": "50.00", "date": "2026-05-10"})
        resp = self.client.post(self._url(), {"amount": "10.00", "date": "2026-05-11"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_the_refund_transaction_un_marks_the_original(self):
        refund_id = self.client.post(
            self._url(), {"amount": "50.00", "date": "2026-05-10"}
        ).data["id"]

        del_resp = self.client.delete(f"/api/v1/transactions/{refund_id}/")
        self.assertEqual(del_resp.status_code, status.HTTP_204_NO_CONTENT)

        original = self.client.get(f"/api/v1/transactions/{self.txn.id}/").data
        self.assertFalse(original["is_refunded"])
        self.assertIsNone(original["refund_transaction_id"])

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(self._url(), {"amount": "50.00", "date": "2026-05-10"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
