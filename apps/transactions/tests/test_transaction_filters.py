"""Filtros de querystring de la lista de transacciones (django-filter)."""
import datetime as dt

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class TransactionFilterTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )
        cls.acc = Wallet.objects.create(
            workspace=cls.ws, name="C", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.salary = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )

        def txn(cat, day, amount, source=Transaction.SOURCE_MANUAL):
            return Transaction.objects.create(
                wallet=cls.acc,
                category=cat,
                amount=amount,
                date=dt.date(2026, 8, day),
                source=source,
            )

        cls.jul = Transaction.objects.create(
            wallet=cls.acc, category=cls.food, amount=10, date=dt.date(2026, 7, 20)
        )
        cls.aug_food = txn(cls.food, 5, 20)
        cls.aug_salary = txn(cls.salary, 1, 1000, source=Transaction.SOURCE_EMAIL_IMPORT)
        cls.sep = Transaction.objects.create(
            wallet=cls.acc, category=cls.food, amount=30, date=dt.date(2026, 9, 2)
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _list(self, **params):
        res = self.client.get(
            "/api/v1/transactions/", params, **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return {r["id"] for r in res.data["results"]}

    def test_date_range_filters_to_the_month(self):
        ids = self._list(date_after="2026-08-01", date_before="2026-08-31")
        self.assertEqual(ids, {str(self.aug_food.id), str(self.aug_salary.id)})

    def test_type_filter_uses_category_type(self):
        ids = self._list(
            date_after="2026-08-01", date_before="2026-08-31", type="income"
        )
        self.assertEqual(ids, {str(self.aug_salary.id)})

    def test_source_filter(self):
        ids = self._list(source="email_import")
        self.assertEqual(ids, {str(self.aug_salary.id)})

    def test_no_filter_returns_all_visible(self):
        self.assertEqual(len(self._list()), 4)
