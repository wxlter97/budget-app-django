"""Convertir una cartera con actividad propia en un grupo: la acción
`split` le pasa todo lo propio a una cuenta nueva, y `validate_parent`
impide agregarle una hija mientras todavía tenga algo propio sin mover."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, InstallmentPurchase, RecurringExpense, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class WalletSplitTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.owner)

        self.multimoney = Wallet.objects.create(
            workspace=self.ws, name="Multimoney", purpose=Wallet.PURPOSE_SAVINGS,
            opening_balance=Decimal("1370.00"), is_default=True,
        )
        self.other = Wallet.objects.create(workspace=self.ws, name="Otra", purpose=Wallet.PURPOSE_SPENDING)
        self.food = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)

        # Actividad propia de Multimoney: como origen, como destino, un
        # recurrente y una compra a plazo (pagada con esa misma cartera).
        self.out_txn = Transaction.objects.create(
            wallet=self.multimoney, category=self.food, amount=Decimal("50.00"),
            date=dt.date(2026, 1, 5),
        )
        self.in_txn = Transaction.objects.create(
            type=Transaction.TYPE_TRANSFER, wallet=self.other, to_wallet=self.multimoney,
            amount=Decimal("200.00"), date=dt.date(2026, 1, 10),
        )
        self.recurring = RecurringExpense.objects.create(
            workspace=self.ws, category=self.food, wallet=self.multimoney,
            amount=Decimal("10.00"), next_due_date=dt.date(2026, 2, 1),
        )
        self.installment = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=self.other, payment_wallet=self.multimoney,
            category=self.food, description="Compra", total_amount=Decimal("300.00"),
            installment_amount=Decimal("100.00"), installments_total=3,
            start_date=dt.date(2026, 1, 1),
        )
        self.multimoney.refresh_from_db()

    def test_cannot_add_a_child_while_it_still_has_its_own_activity(self):
        resp = self.client.patch(
            f"/api/v1/wallets/{self.other.id}/",
            {"parent": str(self.multimoney.id)},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_split_moves_everything_to_the_new_child_and_zeroes_the_parent(self):
        original_balance = self.multimoney.current_balance
        resp = self.client.post(
            f"/api/v1/wallets/{self.multimoney.id}/split/",
            {"name": "Gastos provisionados"},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        child_id = resp.data["child"]["id"]

        self.multimoney.refresh_from_db()
        child = Wallet.objects.get(id=child_id)

        self.assertEqual(child.name, "Gastos provisionados")
        self.assertEqual(child.parent_id, self.multimoney.id)
        self.assertEqual(child.opening_balance, Decimal("1370.00"))
        self.assertEqual(child.current_balance, original_balance)
        self.assertTrue(child.is_default)

        self.assertEqual(self.multimoney.opening_balance, Decimal("0.00"))
        self.assertEqual(self.multimoney.current_balance, Decimal("0.00"))
        self.assertFalse(self.multimoney.is_default)
        # El saldo agregado (propio + hijas) no cambió: ahora es 100% de la hija.
        self.assertEqual(self.multimoney.aggregated_balance, original_balance)

        self.out_txn.refresh_from_db()
        self.assertEqual(self.out_txn.wallet_id, child.id)
        self.in_txn.refresh_from_db()
        self.assertEqual(self.in_txn.to_wallet_id, child.id)
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.wallet_id, child.id)
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.payment_wallet_id, child.id)

        # Ahora sí se le puede agregar otra hija (ya no tiene nada propio).
        resp2 = self.client.patch(
            f"/api/v1/wallets/{self.other.id}/",
            {"parent": str(self.multimoney.id)},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(resp2.status_code, status.HTTP_200_OK, resp2.data)

    def test_split_requires_a_name(self):
        resp = self.client.post(
            f"/api/v1/wallets/{self.multimoney.id}/split/",
            {}, format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_set_parent_on_a_wallet_with_no_activity_of_its_own(self):
        empty = Wallet.objects.create(workspace=self.ws, name="Vacía", purpose=Wallet.PURPOSE_SPENDING)
        resp = self.client.patch(
            f"/api/v1/wallets/{self.other.id}/",
            {"parent": str(empty.id)},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
