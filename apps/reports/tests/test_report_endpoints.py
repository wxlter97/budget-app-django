import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, CategoryBudget, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"

# Reportes que dependen de "hoy" -> los datos se anclan al mes actual.
_FIRST = timezone.localdate().replace(day=1)
YEAR, MONTH = _FIRST.year, _FIRST.month


def day(n):
    return _FIRST.replace(day=n)


def make_workspace(owner, name):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class ReportEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        cls.ws_a = make_workspace(cls.alice, "A")
        cls.ws_b = make_workspace(cls.bob, "B")
        Membership.objects.create(workspace=cls.ws_a, user=cls.bob, role=Membership.ROLE_MEMBER)

        cls.acc = Wallet.objects.create(
            workspace=cls.ws_a, name="C", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("1000.00"),
        )
        cls.salary = Category.objects.create(
            workspace=cls.ws_a, name="Sueldo", type=Category.TYPE_INCOME
        )
        cls.food = Category.objects.create(
            workspace=cls.ws_a, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.transport = Category.objects.create(
            workspace=cls.ws_a, name="Transporte", type=Category.TYPE_EXPENSE
        )

        CategoryBudget.objects.create(
            workspace=cls.ws_a, category=cls.food, amount=Decimal("500.00"),
            month=MONTH, year=YEAR,
        )
        for cat, amount, d in [
            (cls.salary, "3000.00", 1),
            (cls.food, "200.00", 5),
            (cls.food, "150.00", 12),
            (cls.transport, "80.00", 8),
        ]:
            Transaction.objects.create(
                wallet=cls.acc, category=cat, amount=Decimal(amount), date=day(d)
            )
        acc_b = Wallet.objects.create(workspace=cls.ws_b, name="X", purpose=Wallet.PURPOSE_SPENDING)
        cat_b = Category.objects.create(
            workspace=cls.ws_b, name="Otro", type=Category.TYPE_EXPENSE
        )
        Transaction.objects.create(
            wallet=acc_b, category=cat_b, amount=Decimal("999.00"), date=day(5)
        )

    def setUp(self):
        self.client.force_authenticate(self.alice)
        self.client.credentials(**{HEADER: str(self.ws_a.id)})

    def test_header_required(self):
        self.client.credentials()
        self.assertEqual(
            self.client.get("/api/v1/reports/net-worth/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_budget_report(self):
        resp = self.client.get(f"/api/v1/reports/budget/?year={YEAR}&month={MONTH}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {r["category_name"]: r for r in resp.data["rows"]}
        self.assertEqual(rows["Comida"]["budgeted"], "500.00")
        self.assertEqual(rows["Comida"]["spent"], "350.00")
        self.assertEqual(rows["Comida"]["remaining"], "150.00")
        self.assertEqual(rows["Transporte"]["budgeted"], "0.00")
        self.assertEqual(rows["Transporte"]["spent"], "80.00")
        self.assertEqual(resp.data["totals"]["spent"], "430.00")

    def test_budget_report_defaults_to_current_month(self):
        resp = self.client.get("/api/v1/reports/budget/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["month"], MONTH)

    def test_budget_report_rejects_bad_month(self):
        resp = self.client.get("/api/v1/reports/budget/?month=13")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_net_worth_excludes_other_workspace(self):
        resp = self.client.get("/api/v1/reports/net-worth/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["by_purpose"]["spending"], "3570.00")  # 1000 + 3000 - 430
        self.assertEqual(resp.data["net"], "3570.00")

    def test_cashflow_respects_months_param(self):
        resp = self.client.get("/api/v1/reports/cashflow/?months=3")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 3)
        current = resp.data[-1]
        self.assertEqual((current["year"], current["month"]), (YEAR, MONTH))
        self.assertEqual(current["income"], "3000.00")
        self.assertEqual(current["expenses"], "430.00")
        self.assertEqual(current["net"], "2570.00")

    def test_cashflow_months_is_capped(self):
        resp = self.client.get("/api/v1/reports/cashflow/?months=999")
        self.assertEqual(len(resp.data), 24)

    def test_summary_shape(self):
        resp = self.client.get("/api/v1/reports/summary/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for key in ("month", "net_worth", "pending_email_imports", "top_expense_categories"):
            self.assertIn(key, resp.data)
        self.assertEqual(resp.data["top_expense_categories"][0]["category_name"], "Comida")

    def test_private_account_excluded_for_non_owner(self):
        private = Wallet.objects.create(
            workspace=self.ws_a, name="Secreta", purpose=Wallet.PURPOSE_SAVINGS,
            visibility=Wallet.VISIBILITY_PRIVATE, owner=self.alice,
            opening_balance=Decimal("10000.00"),
        )
        Transaction.objects.create(
            wallet=private, category=self.food, amount=Decimal("40.00"), date=day(3)
        )
        self.client.force_authenticate(self.bob)
        self.client.credentials(**{HEADER: str(self.ws_a.id)})
        nw = self.client.get("/api/v1/reports/net-worth/").data
        self.assertEqual(nw["net"], "3570.00")
        budget = self.client.get(
            f"/api/v1/reports/budget/?year={YEAR}&month={MONTH}"
        ).data
        food_row = [r for r in budget["rows"] if r["category_name"] == "Comida"][0]
        self.assertEqual(food_row["spent"], "350.00")
