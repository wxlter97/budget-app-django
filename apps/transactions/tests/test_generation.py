import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, RecurringExpense, Transaction
from apps.transactions.services import generate_recurring_transactions, installment_amounts
from apps.workspaces.models import Workspace


class RecurringExpenseGenerationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.account = Wallet.objects.create(
            workspace=cls.ws, name="C", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.category = Category.objects.create(
            workspace=cls.ws, name="Netflix", type=Category.TYPE_EXPENSE
        )

    def _recurring(self, next_due, **kw):
        return RecurringExpense.objects.create(
            workspace=self.ws, wallet=self.account, category=self.category,
            amount=Decimal("15.00"), next_due_date=next_due, **kw,
        )

    def test_generates_one_transaction_per_overdue_period(self):
        rec = self._recurring(dt.date(2026, 1, 1))
        created = generate_recurring_transactions(as_of=dt.date(2026, 3, 15))
        self.assertEqual(len(created), 3)  # ene, feb, mar
        self.assertTrue(all(t.source == Transaction.SOURCE_RECURRING for t in created))
        rec.refresh_from_db()
        self.assertEqual(rec.next_due_date, dt.date(2026, 4, 1))

    def test_inactive_is_skipped(self):
        self._recurring(dt.date(2026, 1, 1), is_active=False)
        self.assertEqual(generate_recurring_transactions(as_of=dt.date(2026, 2, 1)), [])

    def test_running_twice_does_not_duplicate(self):
        self._recurring(dt.date(2026, 1, 1))
        generate_recurring_transactions(as_of=dt.date(2026, 1, 20))
        generate_recurring_transactions(as_of=dt.date(2026, 1, 20))
        self.assertEqual(Transaction.objects.count(), 1)

    def test_yearly_frequency_advances_a_year(self):
        rec = self._recurring(
            dt.date(2025, 6, 1), frequency=RecurringExpense.FREQUENCY_YEARLY
        )
        created = generate_recurring_transactions(as_of=dt.date(2026, 1, 1))
        self.assertEqual(len(created), 1)
        rec.refresh_from_db()
        self.assertEqual(rec.next_due_date, dt.date(2026, 6, 1))


class InstallmentAmountsTests(TestCase):
    """`installment_amounts`: reparto de una compra a plazo para el estado de
    cuenta -- ceiling por cuota, la última es lo que sobra (ver
    apps.accounts.tests.test_credit_card_statement para el cálculo completo
    anclado a los cortes de la tarjeta)."""

    def test_exact_division_is_equal_across_all_installments(self):
        amounts = installment_amounts(Decimal("1200.00"), 12)
        self.assertEqual(amounts, [Decimal("100.00")] * 12)
        self.assertEqual(sum(amounts, Decimal("0")), Decimal("1200.00"))

    def test_rounds_up_except_the_last_installment(self):
        # 36.73 / 3 = 12.243(3): las dos primeras suben a 12.25, la última
        # absorbe el resto para que la suma cierre exacto.
        amounts = installment_amounts(Decimal("36.73"), 3)
        self.assertEqual(amounts, [Decimal("12.25"), Decimal("12.25"), Decimal("12.23")])
        self.assertEqual(sum(amounts, Decimal("0")), Decimal("36.73"))

    def test_single_installment_is_the_full_total(self):
        self.assertEqual(installment_amounts(Decimal("99.99"), 1), [Decimal("99.99")])
