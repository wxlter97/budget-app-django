"""InstallmentPurchase: una única Transaction al crear, sincronizada con la
compra en cada edición/borrado -- las cuotas son puro cálculo (ver
apps.accounts.tests.test_credit_card_statement para el cálculo en sí)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, InstallmentPurchase, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class InstallmentPurchaseTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, credit_limit=Decimal("3000.00"),
            billing_cycle_day=15,
        )
        self.other_card = Wallet.objects.create(
            workspace=self.ws, name="Mastercard", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, credit_limit=Decimal("3000.00"),
            billing_cycle_day=20,
        )
        self.bank = Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("5000.00"),
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Tecnología", type=Category.TYPE_EXPENSE,
        )
        self.other_cat = Category.objects.create(
            workspace=self.ws, name="Hogar", type=Category.TYPE_EXPENSE,
        )
        self.client.force_authenticate(self.user)

    def _create(self, **over):
        payload = {
            "wallet": str(self.card.id),
            "category": str(self.cat.id),
            "description": "Laptop",
            "total_amount": "1200.00",
            "installments_total": 12,
            "start_date": "2026-09-01",
            **over,
        }
        return self.client.post(
            "/api/v1/installment-purchases/", payload, **{HEADER: str(self.ws.id)}
        )

    def test_create_charges_full_total_to_the_card_once(self):
        res = self._create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("-1200.00"))
        txn = Transaction.objects.get()
        self.assertEqual(txn.type, Transaction.TYPE_EXPENSE)
        self.assertEqual(txn.wallet_id, self.card.id)
        self.assertEqual(txn.category_id, self.cat.id)
        self.assertEqual(txn.amount, Decimal("1200.00"))
        self.assertEqual(txn.date, dt.date(2026, 9, 1))
        self.assertEqual(txn.source, Transaction.SOURCE_INSTALLMENT)
        self.assertEqual(str(txn.installment_purchase_id), res.data["id"])

    def test_requires_a_card_with_billing_cycle_day(self):
        no_cutoff = Wallet.objects.create(
            workspace=self.ws, name="Sin corte", kind=Wallet.KIND_CREDIT,
        )
        res = self._create(wallet=str(no_cutoff.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("wallet", res.data)

    def test_requires_kind_credit(self):
        res = self._create(wallet=str(self.bank.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("wallet", res.data)

    def test_response_has_no_payment_wallet_or_manual_counter(self):
        res = self._create()
        self.assertNotIn("payment_wallet", res.data)
        self.assertNotIn("installment_amount", res.data)
        self.assertIn("installments_paid", res.data)  # computado, solo lectura

    def test_editing_amount_syncs_the_transaction_and_the_balance(self):
        pid = self._create().data["id"]
        res = self.client.patch(
            f"/api/v1/installment-purchases/{pid}/",
            {"total_amount": "1500.00"},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("-1500.00"))
        txn = Transaction.objects.get()
        self.assertEqual(txn.amount, Decimal("1500.00"))

    def test_editing_wallet_moves_the_transaction_and_both_balances(self):
        pid = self._create().data["id"]
        res = self.client.patch(
            f"/api/v1/installment-purchases/{pid}/",
            {"wallet": str(self.other_card.id)},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.card.refresh_from_db()
        self.other_card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("0.00"))
        self.assertEqual(self.other_card.current_balance, Decimal("-1200.00"))

    def test_editing_category_and_description_syncs_the_transaction(self):
        pid = self._create().data["id"]
        self.client.patch(
            f"/api/v1/installment-purchases/{pid}/",
            {"category": str(self.other_cat.id), "description": "Laptop nueva"},
            **{HEADER: str(self.ws.id)},
        )
        txn = Transaction.objects.get()
        self.assertEqual(txn.category_id, self.other_cat.id)
        self.assertIn("Laptop nueva", txn.description)

    def test_deleting_the_purchase_deletes_its_transaction_and_restores_balance(self):
        pid = self._create().data["id"]
        resp = self.client.delete(
            f"/api/v1/installment-purchases/{pid}/", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("0.00"))
        self.assertFalse(Transaction.objects.filter(is_deleted=False).exists())

    def test_deleting_the_generated_transaction_directly_does_not_error(self):
        # La transacción se puede borrar como cualquier otra sin que la
        # compra a plazo (que ya no lleva contador) quede en un estado raro.
        self._create()
        txn = Transaction.objects.get()
        resp = self.client.delete(f"/api/v1/transactions/{txn.id}/", **{HEADER: str(self.ws.id)})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("0.00"))
