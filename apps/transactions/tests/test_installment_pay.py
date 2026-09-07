"""POST /api/v1/installment-purchases/{id}/pay/ — registrar la siguiente cuota."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, InstallmentPurchase, Transaction
from apps.transactions.services import post_next_installment
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class InstallmentPayTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.wallet = Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose=Wallet.PURPOSE_DEBT,
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Tecnología", type=Category.TYPE_EXPENSE,
        )
        self.purchase = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=self.wallet, category=self.cat,
            description="Laptop", total_amount=Decimal("1200.00"),
            installment_amount=Decimal("100.00"), installments_total=12,
            start_date=dt.date(2026, 1, 5),
        )
        self.client.force_authenticate(self.user)

    def test_pay_creates_transaction_and_advances(self):
        res = self.client.post(
            f"/api/v1/installment-purchases/{self.purchase.id}/pay/", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["installments_paid"], 1)
        txn = Transaction.objects.get()
        self.assertEqual(txn.amount, Decimal("100.00"))
        self.assertEqual(txn.source, Transaction.SOURCE_INSTALLMENT)
        self.assertIn("cuota 1/12", txn.description)
        # El pago manual se fecha hoy, no en la fecha teórica del calendario.
        self.assertEqual(txn.date, dt.date.today())

    def test_pay_when_complete_is_400(self):
        self.purchase.installments_paid = 12
        self.purchase.save()
        res = self.client.post(
            f"/api/v1/installment-purchases/{self.purchase.id}/pay/", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_service_returns_none_when_complete(self):
        self.purchase.installments_paid = 12
        self.assertIsNone(post_next_installment(self.purchase, user=self.user))

    def test_deleting_a_cuota_transaction_uncounts_it(self):
        self.client.post(
            f"/api/v1/installment-purchases/{self.purchase.id}/pay/", **{HEADER: str(self.ws.id)}
        )
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.installments_paid, 1)
        txn = Transaction.objects.get()

        resp = self.client.delete(f"/api/v1/transactions/{txn.id}/", **{HEADER: str(self.ws.id)})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.installments_paid, 0)


class CreditCardInstallmentTests(APITestCase):
    """Compra a plazo con tarjeta: cargo total al crear + cuotas como transferencia."""

    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, credit_limit=Decimal("3000.00"),
        )
        self.bank = Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("5000.00"),
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Tecnología", type=Category.TYPE_EXPENSE,
        )
        self.client.force_authenticate(self.user)

    def _create(self, **over):
        payload = {
            "wallet": str(self.card.id),
            "payment_wallet": str(self.bank.id),
            "category": str(self.cat.id),
            "description": "Laptop",
            "total_amount": "1200.00",
            "installment_amount": "100.00",
            "installments_total": 12,
            "start_date": "2026-09-01",
            **over,
        }
        return self.client.post(
            "/api/v1/installment-purchases/", payload, **{HEADER: str(self.ws.id)}
        )

    def test_create_charges_full_total_to_card(self):
        res = self._create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.card.refresh_from_db()
        # el total baja el saldo (crédito disponible) de la tarjeta
        self.assertEqual(self.card.current_balance, Decimal("-1200.00"))
        self.assertEqual(self.card.available_credit, Decimal("1800.00"))
        txn = Transaction.objects.get(source=Transaction.SOURCE_INSTALLMENT)
        self.assertEqual(txn.amount, Decimal("1200.00"))

    def test_pay_is_a_transfer_bank_to_card(self):
        pid = self._create().data["id"]
        res = self.client.post(
            f"/api/v1/installment-purchases/{pid}/pay/", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.card.refresh_from_db()
        self.bank.refresh_from_db()
        # tarjeta: -1200 + 100 ; banco: 5000 - 100
        self.assertEqual(self.card.current_balance, Decimal("-1100.00"))
        self.assertEqual(self.bank.current_balance, Decimal("4900.00"))
        transfer = Transaction.objects.get(type=Transaction.TYPE_TRANSFER)
        self.assertEqual(transfer.wallet_id, self.bank.id)
        self.assertEqual(transfer.to_wallet_id, self.card.id)

    def test_installments_paid_on_create_posts_retroactive_transfers(self):
        # Compra hecha antes de darla de alta en la app: ya se pagaron 4 de
        # las 12 cuotas. Deben quedar registradas (no perderse) para que el
        # saldo de la tarjeta refleje esos pagos, no como si no hubieran
        # pasado.
        res = self._create(installments_paid=4)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["installments_paid"], 4)
        self.card.refresh_from_db()
        self.bank.refresh_from_db()
        # tarjeta: -1200 (cargo total) + 4*100 (cuotas retroactivas)
        self.assertEqual(self.card.current_balance, Decimal("-800.00"))
        self.assertEqual(self.bank.current_balance, Decimal("4600.00"))
        transfers = Transaction.objects.filter(type=Transaction.TYPE_TRANSFER).order_by("date")
        self.assertEqual(transfers.count(), 4)
        self.assertEqual(
            [t.date for t in transfers],
            [dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 11, 1), dt.date(2026, 12, 1)],
        )

    def test_installments_paid_clamped_to_total_on_create(self):
        res = self._create(installments_paid=20)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["installments_paid"], 12)

    def test_payment_wallet_cannot_equal_card(self):
        res = self._create(payment_wallet=str(self.card.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_a_cuota_transaction_uncounts_it(self):
        pid = self._create().data["id"]
        self.client.post(
            f"/api/v1/installment-purchases/{pid}/pay/", **{HEADER: str(self.ws.id)}
        )
        purchase = InstallmentPurchase.objects.get(id=pid)
        self.assertEqual(purchase.installments_paid, 1)
        cuota = Transaction.objects.get(type=Transaction.TYPE_TRANSFER)

        resp = self.client.delete(
            f"/api/v1/transactions/{cuota.id}/", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        purchase.refresh_from_db()
        self.assertEqual(purchase.installments_paid, 0)
        self.card.refresh_from_db()
        self.bank.refresh_from_db()
        # se deshace el efecto en el saldo de ambas carteras, no solo el contador
        self.assertEqual(self.card.current_balance, Decimal("-1200.00"))
        self.assertEqual(self.bank.current_balance, Decimal("5000.00"))

    def test_deleting_the_initial_card_charge_does_not_uncount_cuotas(self):
        pid = self._create().data["id"]
        self.client.post(
            f"/api/v1/installment-purchases/{pid}/pay/", **{HEADER: str(self.ws.id)}
        )
        purchase = InstallmentPurchase.objects.get(id=pid)
        charge = Transaction.objects.get(type=Transaction.TYPE_EXPENSE)

        self.client.delete(f"/api/v1/transactions/{charge.id}/", **{HEADER: str(self.ws.id)})

        purchase.refresh_from_db()
        self.assertEqual(purchase.installments_paid, 1)
