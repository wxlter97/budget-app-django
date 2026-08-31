import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Account
from apps.transactions.models import (
    Category,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)
from apps.transactions.services import (
    generate_due_recurring_expenses,
    post_due_installments,
)
from apps.workspaces.models import Workspace


class RecurringExpenseGenerationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.account = Account.objects.create(
            workspace=cls.ws, name="C", type=Account.TYPE_CHECKING
        )
        cls.category = Category.objects.create(
            workspace=cls.ws, name="Netflix", type=Category.TYPE_EXPENSE
        )

    def _recurring(self, next_due, **kw):
        return RecurringExpense.objects.create(
            workspace=self.ws, account=self.account, category=self.category,
            amount=Decimal("15.00"), next_due_date=next_due, **kw,
        )

    def test_generates_one_transaction_per_overdue_period(self):
        rec = self._recurring(dt.date(2026, 1, 1))
        created = generate_due_recurring_expenses(as_of=dt.date(2026, 3, 15))
        self.assertEqual(len(created), 3)  # ene, feb, mar
        rec.refresh_from_db()
        self.assertEqual(rec.next_due_date, dt.date(2026, 4, 1))

    def test_inactive_is_skipped(self):
        self._recurring(dt.date(2026, 1, 1), is_active=False)
        self.assertEqual(generate_due_recurring_expenses(as_of=dt.date(2026, 2, 1)), [])

    def test_running_twice_does_not_duplicate(self):
        self._recurring(dt.date(2026, 1, 1))
        generate_due_recurring_expenses(as_of=dt.date(2026, 1, 20))
        generate_due_recurring_expenses(as_of=dt.date(2026, 1, 20))
        self.assertEqual(Transaction.objects.count(), 1)

    def test_yearly_frequency_advances_a_year(self):
        rec = self._recurring(
            dt.date(2025, 6, 1), frequency=RecurringExpense.FREQUENCY_YEARLY
        )
        created = generate_due_recurring_expenses(as_of=dt.date(2026, 1, 1))
        self.assertEqual(len(created), 1)
        rec.refresh_from_db()
        self.assertEqual(rec.next_due_date, dt.date(2026, 6, 1))


class InstallmentGenerationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.account = Account.objects.create(
            workspace=cls.ws, name="Tarjeta", type=Account.TYPE_CREDIT
        )
        cls.category = Category.objects.create(
            workspace=cls.ws, name="Electro", type=Category.TYPE_EXPENSE
        )

    def _purchase(self, **kw):
        defaults = dict(
            workspace=self.ws, account=self.account, category=self.category,
            description="Lavadora", total_amount=Decimal("1200.00"),
            installment_amount=Decimal("100.00"), installments_total=12,
            start_date=dt.date(2026, 1, 5),
        )
        defaults.update(kw)
        return InstallmentPurchase.objects.create(**defaults)

    def test_posts_due_installments_and_increments(self):
        p = self._purchase()
        created = post_due_installments(as_of=dt.date(2026, 3, 10))
        self.assertEqual(len(created), 3)
        p.refresh_from_db()
        self.assertEqual(p.installments_paid, 3)

    def test_running_twice_does_not_duplicate(self):
        self._purchase()
        post_due_installments(as_of=dt.date(2026, 2, 10))
        post_due_installments(as_of=dt.date(2026, 2, 10))
        self.assertEqual(Transaction.objects.count(), 2)

    def test_stops_at_total_installments(self):
        p = self._purchase(installments_total=2)
        post_due_installments(as_of=dt.date(2030, 1, 1))
        p.refresh_from_db()
        self.assertEqual(p.installments_paid, 2)
        self.assertTrue(p.is_completed)
