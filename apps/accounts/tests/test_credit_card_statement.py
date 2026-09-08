"""Pago de contado de una tarjeta de crédito a una fecha:

    pago_de_contado = saldo_usado (= límite - disponible)
                    - capital a plazo aún no vencido

Una compra a plazo es una única Transaction (el total, el día de la
compra); "capital aún no vencido" es puro cálculo sobre esa compra y los
cortes de la tarjeta (ver `installment_status`), no depende de ningún
registro adicional.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import (
    credit_card_statement,
    credit_card_statements_summary,
    recompute_wallet_balance,
)
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
        kwargs.setdefault("credit_limit", Decimal("5000.00"))
        return Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT, **kwargs
        )

    def _expense(self, w, amount, date, **kw):
        return Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal(amount), date=date, **kw
        )

    def _payment(self, w, amount, date):
        return Transaction.objects.create(
            type=Transaction.TYPE_TRANSFER, wallet=self.checking, to_wallet=w,
            amount=Decimal(amount), date=date,
        )

    # -- fechas / corte ---------------------------------------------------

    def test_none_for_non_credit_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", kind=Wallet.KIND_BANK)
        self.assertIsNone(credit_card_statement(w))

    def test_none_without_billing_cycle_day(self):
        self.assertIsNone(credit_card_statement(self._card()))

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
        data = credit_card_statement(w, as_of=dt.date(2024, 2, 29))
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

    # -- pago de contado ------------------------------------------------

    def test_used_and_available_from_balance(self):
        w = self._card(billing_cycle_day=3, credit_limit=Decimal("2000.00"))
        self._expense(w, "100.00", dt.date(2024, 1, 1))
        recompute_wallet_balance(w)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["used"], Decimal("100.00"))
        self.assertEqual(data["available"], Decimal("1900.00"))
        self.assertEqual(data["total_due"], Decimal("100.00"))

    def test_available_null_without_credit_limit(self):
        w = self._card(billing_cycle_day=3, credit_limit=None)
        self._expense(w, "100.00", dt.date(2024, 1, 1))
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertIsNone(data["available"])
        self.assertIsNone(data["credit_limit"])
        self.assertEqual(data["total_due"], Decimal("100.00"))

    def test_opening_balance_debt_counts_as_used(self):
        w = self._card(billing_cycle_day=3, opening_balance=Decimal("-200.00"))
        self._expense(w, "50.00", dt.date(2024, 1, 1))
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 5))
        self.assertEqual(data["total_due"], Decimal("250.00"))

    def test_payments_and_income_reduce_used(self):
        w = self._card(billing_cycle_day=3)
        self._expense(w, "120.00", dt.date(2024, 1, 1))
        self._payment(w, "300.00", dt.date(2024, 1, 2))
        Transaction.objects.create(  # reverso acreditado a la tarjeta
            wallet=w, type=Transaction.TYPE_INCOME, category=None,
            amount=Decimal("45.00"), date=dt.date(2024, 1, 2),
        )
        recompute_wallet_balance(w)
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 3))
        self.assertEqual(data["total_due"], -w.current_balance)
        self.assertEqual(data["total_due"], Decimal("-225.00"))

    def test_used_is_as_of_not_frozen_at_cutoff(self):
        w = self._card(billing_cycle_day=3)
        self._expense(w, "100.00", dt.date(2024, 1, 1))
        self._payment(w, "40.00", dt.date(2024, 1, 10))  # después del corte del 3
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 15))
        self.assertEqual(data["total_due"], Decimal("60.00"))

    # -- compras a plazo ----------------------------------------------

    def _purchase(self, w, *, total=Decimal("1200.00"), n=12, start=dt.date(2024, 1, 10)):
        """Como la crearía la API: la compra + su única Transaction (el
        total, contra `w`, el día `start`)."""
        p = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=w, category=self.expense_cat,
            description="Laptop", total_amount=total, installments_total=n,
            start_date=start,
        )
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=total,
            description="Laptop (compra a plazo)", date=start,
            source=Transaction.SOURCE_INSTALLMENT, installment_purchase=p,
        )
        return p

    def test_installment_not_due_amounts_are_excluded_from_total_due(self):
        w = self._card(billing_cycle_day=3, credit_limit=Decimal("2000.00"))
        self._purchase(w, start=dt.date(2023, 12, 10))  # 1200 / 12 cuotas de 100
        recompute_wallet_balance(w)
        data = credit_card_statement(w, as_of=dt.date(2024, 4, 20))
        # corte del 3 de abril: la 1.ª cuota cae en el corte del 3-ene (la
        # compra del 10-dic ya pasó el corte del 3-dic) -> vencidas 1..4.
        self.assertEqual(data["used"], Decimal("1200.00"))
        self.assertEqual(data["available"], Decimal("800.00"))
        self.assertEqual(data["installments_not_due"], Decimal("800.00"))  # 8 * 100
        self.assertEqual(data["total_due"], Decimal("400.00"))

    def test_installment_fully_due_excludes_nothing(self):
        w = self._card(billing_cycle_day=3)
        self._purchase(w, total=Decimal("600.00"), n=6, start=dt.date(2023, 1, 10))
        recompute_wallet_balance(w)
        data = credit_card_statement(w, as_of=dt.date(2024, 6, 1))
        self.assertEqual(data["installments_not_due"], Decimal("0"))
        self.assertEqual(data["installment_lines"], [])
        self.assertEqual(data["total_due"], Decimal("600.00"))

    def test_installment_lines_use_the_ceiling_schedule(self):
        w = self._card(billing_cycle_day=3)
        # 100.01 / 3: dos cuotas de 33.34 (ceiling), la última 33.33 (resto).
        self._purchase(w, total=Decimal("100.01"), n=3, start=dt.date(2024, 1, 10))
        # ningún corte pasó todavía -> las 3 cuotas están pendientes
        data = credit_card_statement(w, as_of=dt.date(2024, 1, 15))
        self.assertEqual(len(data["installment_lines"]), 1)
        line = data["installment_lines"][0]
        self.assertEqual(line["installments_pending"], 3)
        self.assertEqual(line["installments_total"], 3)
        self.assertEqual(line["amount_pending"], Decimal("100.01"))
        self.assertEqual(data["installments_not_due"], Decimal("100.01"))


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
            workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT,
            billing_cycle_day=3, credit_limit=Decimal("2000.00"),
        )
        Transaction.objects.create(
            wallet=w, category=self.expense_cat, amount=Decimal("100.00"), date=dt.date(2024, 1, 1)
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/statement/?as_of=2024-01-05")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["cutoff_date"], "2024-01-03")
        self.assertEqual(resp.data["used"], "100.00")
        self.assertEqual(resp.data["available"], "1900.00")
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
        Wallet.objects.create(workspace=self.ws, name="Sin corte", kind=Wallet.KIND_CREDIT)
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
