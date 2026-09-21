"""`GET /transactions/totals/` y la paginación estable de la lista."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Tag, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class TotalsAndPagingTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.usd = Wallet.objects.create(workspace=cls.ws, name="USD", purpose=Wallet.PURPOSE_SPENDING)
        cls.eur = Wallet.objects.create(
            workspace=cls.ws, name="EUR", purpose=Wallet.PURPOSE_SPENDING, currency="EUR"
        )
        cls.food = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)
        cls.pay = Category.objects.create(workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME)
        cls.tag = Tag.objects.create(workspace=cls.ws, name="viaje")

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _txn(self, wallet, cat, amount, day=1, **kw):
        return Transaction.objects.create(
            wallet=wallet, category=cat, amount=Decimal(amount), date=dt.date(2026, 8, day), **kw
        )

    def _totals(self, **params):
        res = self.client.get("/api/v1/transactions/totals/", params, **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return {r["currency"]: r for r in res.data}

    def test_sums_income_and_expenses_per_currency(self):
        self._txn(self.usd, self.pay, "1000")
        self._txn(self.usd, self.food, "30")
        self._txn(self.usd, self.food, "20.50")
        self._txn(self.eur, self.food, "5")
        totals = self._totals()
        self.assertEqual(Decimal(totals["USD"]["income"]), Decimal("1000"))
        self.assertEqual(Decimal(totals["USD"]["expenses"]), Decimal("50.50"))
        self.assertEqual(Decimal(totals["EUR"]["expenses"]), Decimal("5"))
        self.assertEqual(Decimal(totals["EUR"]["income"]), Decimal("0"))

    def test_uses_the_same_filters_as_the_list(self):
        t = self._txn(self.usd, self.food, "40")
        t.tags.add(self.tag)
        self._txn(self.usd, self.food, "7")
        self.assertEqual(Decimal(self._totals(tag=str(self.tag.id))["USD"]["expenses"]), Decimal("40"))
        self.assertEqual(Decimal(self._totals(category=str(self.food.id))["USD"]["expenses"]), Decimal("47"))

    def test_respects_counts_toward_budget_filter(self):
        self._txn(self.usd, self.food, "10")
        self._txn(self.usd, self.food, "90", counts_toward_budget=False)
        self.assertEqual(
            Decimal(self._totals(counts_toward_budget="true")["USD"]["expenses"]), Decimal("10")
        )

    def test_transfers_are_not_income_or_expense(self):
        Transaction.objects.create(
            wallet=self.usd, to_wallet=self.eur, amount=Decimal("100"),
            date=dt.date(2026, 8, 1), type=Transaction.TYPE_TRANSFER,
        )
        self.assertEqual(self._totals(), {})

    def test_other_workspaces_are_not_counted(self):
        other = Workspace.objects.create(name="Otro")
        w = Wallet.objects.create(workspace=other, name="X", purpose=Wallet.PURPOSE_SPENDING)
        c = Category.objects.create(workspace=other, name="C", type=Category.TYPE_EXPENSE)
        Transaction.objects.create(wallet=w, category=c, amount=Decimal("99"), date=dt.date(2026, 8, 1))
        self.assertEqual(self._totals(), {})

    def test_pages_do_not_repeat_or_skip_rows_with_equal_dates(self):
        # Todas el mismo día y creadas casi a la vez: el desempate por id evita que
        # el orden cambie entre una página y la siguiente.
        for i in range(12):
            self._txn(self.usd, self.food, "1", description=f"g{i}")
        seen = []
        url = "/api/v1/transactions/?limit=5"
        while url:
            res = self.client.get(url, **{HEADER: str(self.ws.id)})
            seen += [r["id"] for r in res.data["results"]]
            url = res.data["next"]
        self.assertEqual(len(seen), 12)
        self.assertEqual(len(set(seen)), 12)


class CompressionTests(APITestCase):
    def test_json_is_gzipped_when_the_client_accepts_it(self):
        user = User.objects.create_user("bob", "b@example.com", "pw")
        ws = Workspace.objects.create(name="B")
        Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_OWNER)
        w = Wallet.objects.create(workspace=ws, name="C", purpose=Wallet.PURPOSE_SPENDING)
        c = Category.objects.create(workspace=ws, name="Comida", type=Category.TYPE_EXPENSE)
        for i in range(20):
            Transaction.objects.create(
                wallet=w, category=c, amount=Decimal("1"), date=dt.date(2026, 8, 1), description=f"g{i}"
            )
        self.client.force_authenticate(user)
        res = self.client.get(
            "/api/v1/transactions/", HTTP_ACCEPT_ENCODING="gzip", **{HEADER: str(ws.id)}
        )
        self.assertEqual(res["Content-Encoding"], "gzip")
        plain = self.client.get("/api/v1/transactions/", **{HEADER: str(ws.id)})
        self.assertNotIn("Content-Encoding", plain)


class DashboardSummaryQueriesTests(APITestCase):
    def test_reads_the_exchange_rates_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        user = User.objects.create_user("carol", "c@example.com", "pw")
        ws = Workspace.objects.create(name="C")
        Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get("/api/v1/reports/summary/", **{HEADER: str(ws.id)})
        self.assertEqual(res.status_code, 200)
        rate_reads = [q for q in ctx.captured_queries if "workspaces_exchangerate" in q["sql"]]
        self.assertEqual(len(rate_reads), 1)
