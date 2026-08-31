import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Account
from apps.accounts.services import recompute_account_balance
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Workspace


def money(x):
    return Decimal(x).quantize(Decimal("0.01"))


class BalanceSignalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.income_cat = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )
        cls.expense_cat = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def _account(self, opening="100.00"):
        return Account.objects.create(
            workspace=self.ws, name="C", type=Account.TYPE_CHECKING,
            opening_balance=Decimal(opening),
        )

    def _txn(self, account, cat, amount, **kw):
        return Transaction.objects.create(
            account=account, category=cat, amount=Decimal(amount),
            date=dt.date(2026, 1, 10), **kw,
        )

    def _bal(self, account):
        account.refresh_from_db()
        return account.current_balance

    def test_opening_balance_seeds_current_balance(self):
        acc = self._account("250.00")
        self.assertEqual(self._bal(acc), money("250.00"))

    def test_income_and_expense_move_balance(self):
        acc = self._account("100.00")
        self._txn(acc, self.income_cat, "40.00")
        self.assertEqual(self._bal(acc), money("140.00"))
        self._txn(acc, self.expense_cat, "25.00")
        self.assertEqual(self._bal(acc), money("115.00"))

    def test_editing_amount_adjusts_balance(self):
        acc = self._account("0.00")
        txn = self._txn(acc, self.expense_cat, "30.00")
        self.assertEqual(self._bal(acc), money("-30.00"))
        txn.amount = Decimal("50.00")
        txn.save()
        self.assertEqual(self._bal(acc), money("-50.00"))

    def test_changing_category_type_flips_effect(self):
        acc = self._account("0.00")
        txn = self._txn(acc, self.expense_cat, "20.00")
        self.assertEqual(self._bal(acc), money("-20.00"))
        txn.category = self.income_cat
        txn.save()
        self.assertEqual(self._bal(acc), money("20.00"))

    def test_moving_transaction_between_accounts(self):
        a = self._account("0.00")
        b = self._account("0.00")
        txn = self._txn(a, self.expense_cat, "15.00")
        self.assertEqual(self._bal(a), money("-15.00"))
        txn.account = b
        txn.save()
        self.assertEqual(self._bal(a), money("0.00"))
        self.assertEqual(self._bal(b), money("-15.00"))

    def test_soft_delete_reverses_and_restore_reapplies(self):
        acc = self._account("100.00")
        txn = self._txn(acc, self.expense_cat, "40.00")
        self.assertEqual(self._bal(acc), money("60.00"))
        txn.soft_delete()
        self.assertEqual(self._bal(acc), money("100.00"))
        txn.is_deleted = False
        txn.save()
        self.assertEqual(self._bal(acc), money("60.00"))

    def test_instance_delete_reverses(self):
        acc = self._account("100.00")
        txn = self._txn(acc, self.income_cat, "10.00")
        self.assertEqual(self._bal(acc), money("110.00"))
        txn.delete()
        self.assertEqual(self._bal(acc), money("100.00"))

    def test_recompute_fixes_drift(self):
        acc = self._account("100.00")
        self._txn(acc, self.expense_cat, "30.00")
        Account.objects.filter(pk=acc.pk).update(current_balance=Decimal("999.00"))
        recompute_account_balance(acc)
        self.assertEqual(self._bal(acc), money("70.00"))
