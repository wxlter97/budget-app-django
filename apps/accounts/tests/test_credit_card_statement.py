"""Estado de cuenta de tarjeta de crédito: "cuánto debo a esta fecha, y
cuánto tengo que transferir para estar al día" (Fase 3 del roadmap)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import credit_card_statement, credit_card_statements_summary
from apps.transactions.models import Category, InstallmentPurchase, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class CreditCardStatementServiceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.expense_cat = Category.objects.create(
            workspace=cls.ws, name="Compras", type=Category.TYPE_EXPENSE
        )
        cls.checking = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", kind=Wallet.KIND_BANK
        )

    def _card(self, **kwargs):
        return Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("5000.00"), **kwargs,
        )

    def test_none_for_non_credit_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", kind=Wallet.KIND_BANK)
        self.assertIsNone(credit_card_statement(w))

    def test_none_without_billing_cycle_day(self):
        w = self._card()
        self.assertIsNone(credit_card_statement(w))

    def test_cutoff_before_billing_day_falls_back_to_previous_month(self):
        w = self._card(billing_cycle_day=3)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 2))
        self.assertEqual(data["cutoff_date"], dt.date(2023, 12, 3))
        self.assertEqual(data["next_cutoff_date"], dt.date(2024, 1, 3))

    def test_cutoff_on_or_after_billing_day_uses_current_month(self):
        w = self._card(billing_cycle_day=3)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["cutoff_date"], dt.date(2024, 1, 3))
        self.assertEqual(data["next_cutoff_date"], dt.date(2024, 2, 3))

    def test_cutoff_day_clamped_to_end_of_short_month(self):
        w = self._card(billing_cycle_day=31)
        data = credit_card_statement(w, as_of=dt.date(2024, 2, 29))  # 2024 es bisiesto
        self.assertEqual(data["cutoff_date"], dt.date(2024, 2, 29))

    def test_payment_due_date_next_month_when_due_day_before_cutoff_day(self):
        w = self._card(billing_cycle_day=25, payment_due_day=10)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 26))
        self.assertEqual(data["cutoff_date"], dt.date(2024, 1, 25))
        self.assertEqual(data["payment_due_date"], dt.date(2024, 2, 10))

    def test_payment_due_date_same_month_when_due_day_after_cutoff_day(self):
        w = self._card(billing_cycle_day=3, payment_due_day=20)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["payment_due_date"], dt.date(2024, 1, 20))

    def test_expenses_up_to_cutoff_minus_payments(self):
        w = self._card(billing_cycle_day=3)
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal("100.00"), date=dt.date(2024, 1, 1)
        )
        Transaction.objects.create(
            type=Transaction.TYPE_TRANSFER, wallet=self.checking, to_wallet=w,
            amount=Decimal("40.00"), date=dt.date(2024, 1, 2),
        )
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["spent"], Decimal("100.00"))
        self.assertEqual(data["paid"], Decimal("40.00"))
        self.assertEqual(data["total_due"], Decimal("60.00"))

    def test_current_period_activity_not_yet_due(self):
        w = self._card(billing_cycle_day=3)
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal("100.00"), date=dt.date(2024, 1, 1)
        )
        # Gasto DESPUÉS del corte de enero: todavía no vence, no debe sumar a total_due.
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal("30.00"), date=dt.date(2024, 1, 4)
        )
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["total_due"], Decimal("100.00"))
        self.assertEqual(data["current_period_spent"], Decimal("30.00"))
        self.assertEqual(data["current_period_paid"], Decimal("0"))

    def test_installment_full_charge_excluded_but_due_cuotas_added(self):
        w = self._card(billing_cycle_day=3)
        purchase = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=w, payment_wallet=self.checking, category=self.expense_cat,
            description="Laptop", total_amount=Decimal("1200.00"), installment_amount=Decimal("100.00"),
            installments_total=12, start_date=dt.date(2024, 1, 10),
        )
        # El cargo total se registra de una sola vez el día de la compra (así
        # funciona `services.post_initial_installment_charge` en transactions).
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=purchase.total_amount,
            description="Laptop (compra a 12 cuotas)", date=purchase.start_date,
            source=Transaction.SOURCE_INSTALLMENT,
        )
        # A la fecha del corte de febrero (3), solo venció la cuota 1 (10 ene).
        data = credit_card_statement(w, as_of=dt.date(2024, 2, 5))
        self.assertEqual(data["cutoff_date"], dt.date(2024, 2, 3))
        self.assertEqual(data["spent"], Decimal("0"))  # el cargo de 1200 no cuenta como gasto
        self.assertEqual(data["installments_due"], Decimal("100.00"))
        self.assertEqual(data["total_due"], Decimal("100.00"))
        self.assertEqual(len(data["installment_lines"]), 1)
        self.assertEqual(data["installment_lines"][0]["installments_due"], 1)

    def test_unpaid_installment_cuota_carries_forward_to_next_cutoff(self):
        w = self._card(billing_cycle_day=3)
        InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=w, payment_wallet=self.checking, category=self.expense_cat,
            description="Laptop", total_amount=Decimal("1200.00"), installment_amount=Decimal("100.00"),
            installments_total=12, start_date=dt.date(2024, 1, 10),
        )
        # Sin abonos: en marzo (corte del 3) ya vencieron las cuotas 1 (10 ene)
        # y 2 (10 feb) -- ambas siguen pendientes.
        data = credit_card_statement(w, as_of=dt.date(2024, 3, 5))
        self.assertEqual(data["installments_due"], Decimal("200.00"))
        self.assertEqual(data["total_due"], Decimal("200.00"))

    def test_paying_a_cutoff_clears_it_and_only_new_cuota_remains_due(self):
        w = self._card(billing_cycle_day=3)
        InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=w, payment_wallet=self.checking, category=self.expense_cat,
            description="Laptop", total_amount=Decimal("1200.00"), installment_amount=Decimal("100.00"),
            installments_total=12, start_date=dt.date(2024, 1, 10),
        )
        # Se abona lo que se debía del corte de febrero (100, la cuota 1).
        Transaction.objects.create(
            type=Transaction.TYPE_TRANSFER, wallet=self.checking, to_wallet=w,
            amount=Decimal("100.00"), date=dt.date(2024, 2, 15),
        )
        data = credit_card_statement(w, as_of=dt.date(2024, 3, 5))
        self.assertEqual(data["installments_due"], Decimal("200.00"))  # cuotas 1 y 2, acumuladas
        self.assertEqual(data["paid"], Decimal("100.00"))
        self.assertEqual(data["total_due"], Decimal("100.00"))  # solo la cuota 2, nueva


class CreditCardStatementApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.expense_cat = Category.objects.create(
            workspace=cls.ws, name="Compras", type=Category.TYPE_EXPENSE
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_statement_endpoint_shape(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT, billing_cycle_day=3,
        )
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal("100.00"), date=dt.date(2024, 1, 1)
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/statement/?as_of=2024-01-05")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["cutoff_date"], "2024-01-03")
        self.assertEqual(resp.data["total_due"], "100.00")

    def test_statement_404_for_non_credit_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", kind=Wallet.KIND_BANK)
        resp = self.client.get(f"/api/v1/wallets/{w.id}/statement/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_statement_400_for_invalid_as_of(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT, billing_cycle_day=3,
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/statement/?as_of=not-a-date")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_statements_summary_lists_only_configured_credit_cards(self):
        good = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT, billing_cycle_day=3,
        )
        Wallet.objects.create(
            workspace=self.ws, name="Sin corte", kind=Wallet.KIND_CREDIT,
        )
        Wallet.objects.create(workspace=self.ws, name="Banco", kind=Wallet.KIND_BANK)
        resp = self.client.get("/api/v1/wallets/statements/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["wallet_id"], str(good.id))

    def test_statements_summary_excludes_other_workspace(self):
        other_ws = Workspace.objects.create(name="B")
        Membership.objects.create(workspace=other_ws, user=self.user, role=Membership.ROLE_OWNER)
        Wallet.objects.create(
            workspace=other_ws, name="Otra tarjeta", kind=Wallet.KIND_CREDIT, billing_cycle_day=3,
        )
        resp = self.client.get("/api/v1/wallets/statements/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 0)
