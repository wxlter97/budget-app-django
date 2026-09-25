"""Estado de cuenta por ciclo (como lo imprime el banco), pago mínimo,
interés estimado, lo que va al próximo estado, y el resumen de un período de
cualquier cartera (saldo inicial → final, vs. el período anterior)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import (
    minimum_payment,
    statement_cycle,
    statement_cycles,
    unbilled_activity,
    wallet_period_summary,
)
from apps.transactions.models import Category, InstallmentPurchase, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class _Base(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.cat = Category.objects.create(workspace=cls.ws, name="Compras", type=Category.TYPE_EXPENSE)
        cls.bank = Wallet.objects.create(workspace=cls.ws, name="Cuenta", kind=Wallet.KIND_BANK)

    def _card(self, **kw):
        kw.setdefault("billing_cycle_day", 1)
        kw.setdefault("payment_due_day", 10)
        return Wallet.objects.create(workspace=self.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT, **kw)

    def _buy(self, w, amount, date, **kw):
        return Transaction.objects.create(
            wallet=w, category=self.cat, amount=Decimal(amount), date=date, **kw
        )

    def _pay(self, w, amount, date):
        return Transaction.objects.create(
            type=Transaction.TYPE_TRANSFER, wallet=self.bank, to_wallet=w,
            amount=Decimal(amount), date=date,
        )


class StatementCycleTests(_Base):
    def test_cycle_reconciles_like_a_bank_statement(self):
        w = self._card()
        self._buy(w, "100.00", dt.date(2026, 1, 20))  # ciclo de febrero-01: saldo anterior
        self._pay(w, "40.00", dt.date(2026, 2, 5))  # pago dentro del ciclo siguiente
        self._buy(w, "70.00", dt.date(2026, 2, 15))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 3))
        self.assertEqual(c["period_start"], dt.date(2026, 2, 2))
        self.assertEqual(c["previous_balance"], Decimal("100.00"))
        self.assertEqual(c["purchases"], Decimal("70.00"))
        self.assertEqual(c["payments"], Decimal("40.00"))
        self.assertEqual(c["statement_balance"], Decimal("130.00"))
        self.assertEqual(c["adjustments"], Decimal("0.00"))
        self.assertEqual(c["payment_due_date"], dt.date(2026, 3, 10))
        self.assertEqual(c["status"], "pending")

    def test_installment_purchase_enters_one_installment_per_cycle(self):
        w = self._card()
        purchase = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=w, category=self.cat, description="TV",
            total_amount=Decimal("300.00"), installments_total=3, start_date=dt.date(2026, 2, 10),
        )
        self._buy(w, "300.00", dt.date(2026, 2, 10), installment_purchase=purchase,
                  source=Transaction.SOURCE_INSTALLMENT)
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 2))
        self.assertEqual(c["purchases"], Decimal("0"))
        self.assertEqual(c["installments_charged"], Decimal("100.00"))
        self.assertEqual(c["statement_balance"], Decimal("100.00"))
        self.assertEqual(c["adjustments"], Decimal("0.00"))

    def test_paid_after_cutoff_marks_cycle_paid(self):
        w = self._card()
        self._buy(w, "80.00", dt.date(2026, 2, 15))
        self._pay(w, "80.00", dt.date(2026, 3, 4))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 5))
        self.assertEqual(c["paid_since_cutoff"], Decimal("80.00"))
        self.assertEqual(c["remaining"], Decimal("0"))
        self.assertEqual(c["status"], "paid")

    def test_overdue_after_due_date_without_payment(self):
        w = self._card()
        self._buy(w, "80.00", dt.date(2026, 2, 15))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 11))
        self.assertEqual(c["status"], "overdue")

    def test_minimum_paid_after_due_date_is_not_overdue(self):
        w = self._card(min_payment_pct=Decimal("10"), min_payment_floor=Decimal("5"))
        self._buy(w, "200.00", dt.date(2026, 2, 15))
        self._pay(w, "20.00", dt.date(2026, 3, 8))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 12))
        self.assertEqual(c["minimum_payment"], Decimal("20.00"))
        self.assertEqual(c["minimum_remaining"], Decimal("0.00"))
        self.assertEqual(c["status"], "minimum_paid")

    def test_payments_after_due_date_do_not_count_for_that_cycle(self):
        w = self._card()
        self._buy(w, "80.00", dt.date(2026, 2, 15))
        self._pay(w, "80.00", dt.date(2026, 3, 15))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 20))
        self.assertEqual(c["paid_since_cutoff"], Decimal("0"))
        self.assertEqual(c["status"], "overdue")

    def test_interest_estimates(self):
        w = self._card(interest_rate=Decimal("24.00"), min_payment_pct=Decimal("10"))
        self._buy(w, "1000.00", dt.date(2026, 2, 15))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 2))
        # 24% anual = 2% mensual.
        self.assertEqual(c["interest_if_minimum"], Decimal("18.00"))  # sobre 900
        self.assertEqual(c["interest_if_unpaid"], Decimal("20.00"))  # sobre 1000

    def test_no_minimum_or_interest_without_configuration(self):
        w = self._card()
        self._buy(w, "100.00", dt.date(2026, 2, 15))
        c = statement_cycle(w, dt.date(2026, 3, 1), as_of=dt.date(2026, 3, 2))
        self.assertIsNone(c["minimum_payment"])
        self.assertIsNone(c["interest_if_minimum"])
        self.assertIsNone(c["interest_if_unpaid"])

    def test_cycles_most_recent_first(self):
        w = self._card()
        cycles = statement_cycles(w, count=3, as_of=dt.date(2026, 3, 5))
        self.assertEqual(
            [c["cutoff_date"] for c in cycles],
            [dt.date(2026, 3, 1), dt.date(2026, 2, 1), dt.date(2026, 1, 1)],
        )

    def test_unbilled_goes_to_next_statement(self):
        w = self._card()
        self._buy(w, "80.00", dt.date(2026, 2, 15))  # del corte
        self._buy(w, "25.00", dt.date(2026, 3, 3))  # después del corte
        u = unbilled_activity(w, as_of=dt.date(2026, 3, 5))
        self.assertEqual(u["purchases"], Decimal("25.00"))
        self.assertEqual(u["next_cutoff_date"], dt.date(2026, 4, 1))


class MinimumPaymentTests(_Base):
    def test_floor_wins_over_small_percentage(self):
        w = self._card(min_payment_pct=Decimal("5"), min_payment_floor=Decimal("25"))
        self.assertEqual(minimum_payment(w, Decimal("100.00")), Decimal("25.00"))

    def test_never_more_than_balance(self):
        w = self._card(min_payment_floor=Decimal("25"))
        self.assertEqual(minimum_payment(w, Decimal("10.00")), Decimal("10.00"))


class WalletPeriodSummaryTests(_Base):
    def test_opening_flows_closing_and_previous_month(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Ahorro", kind=Wallet.KIND_BANK, opening_balance=Decimal("100.00")
        )
        Transaction.objects.create(wallet=w, type=Transaction.TYPE_INCOME, amount=Decimal("50.00"),
                                   date=dt.date(2026, 8, 15))
        self._buy(w, "30.00", dt.date(2026, 9, 3))
        Transaction.objects.create(type=Transaction.TYPE_TRANSFER, wallet=self.bank, to_wallet=w,
                                   amount=Decimal("20.00"), date=dt.date(2026, 9, 10))
        s = wallet_period_summary(w, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
        self.assertEqual(s["opening_balance"], Decimal("150.00"))
        self.assertEqual(s["inflows"], Decimal("20.00"))
        self.assertEqual(s["outflows"], Decimal("30.00"))
        self.assertEqual(s["closing_balance"], Decimal("140.00"))
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["previous"]["date_after"], dt.date(2026, 8, 1))
        self.assertEqual(s["previous"]["date_before"], dt.date(2026, 8, 31))
        self.assertEqual(s["previous"]["inflows"], Decimal("50.00"))

    def test_arbitrary_range_compares_with_same_length_before(self):
        w = Wallet.objects.create(workspace=self.ws, name="X", kind=Wallet.KIND_BANK)
        s = wallet_period_summary(w, dt.date(2026, 9, 11), dt.date(2026, 9, 20))
        self.assertEqual(s["previous"]["date_after"], dt.date(2026, 9, 1))
        self.assertEqual(s["previous"]["date_before"], dt.date(2026, 9, 10))


class StatementCyclesApiTests(_Base):
    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_statement_cycles_endpoint(self):
        w = self._card()
        resp = self.client.get(f"/api/v1/wallets/{w.id}/statement-cycles/?count=2")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["cycles"]), 2)
        self.assertIn("unbilled", resp.data)

    def test_statement_cycles_404_for_non_card(self):
        resp = self.client.get(f"/api/v1/wallets/{self.bank.id}/statement-cycles/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_period_summary_endpoint(self):
        resp = self.client.get(
            f"/api/v1/wallets/{self.bank.id}/period-summary/?date_after=2026-09-01&date_before=2026-09-30"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["previous"]["date_after"], "2026-08-01")

    def test_period_summary_requires_dates(self):
        resp = self.client.get(f"/api/v1/wallets/{self.bank.id}/period-summary/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_min_payment_pct_validated(self):
        w = self._card()
        resp = self.client.patch(f"/api/v1/wallets/{w.id}/", {"min_payment_pct": "150"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_statements_summary_includes_remaining_of_last_cutoff(self):
        w = self._card()
        self._buy(w, "80.00", dt.date(2020, 1, 15))
        resp = self.client.get("/api/v1/wallets/statements/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = next(r for r in resp.data if str(r["wallet_id"]) == str(w.id))
        self.assertIn(row["status"], {"pending", "overdue", "paid", "minimum_paid", "nothing_due"})
        self.assertIn("remaining", row)
