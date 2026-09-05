from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.reports.services import (
    budget_vs_actual,
    monthly_cashflow,
    net_worth_breakdown,
    spending_by_category,
)
from apps.transactions.models import Category, CategoryBudget, Transaction
from apps.workspaces.currency import convert, get_rate_map
from apps.workspaces.models import ExchangeRate, Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def make_workspace(owner, name="Casa", base_currency="USD"):
    ws = Workspace.objects.create(name=name, base_currency=base_currency)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class CurrencyHelperTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.ws = make_workspace(self.user)

    def test_rate_map_includes_base_at_one(self):
        self.assertEqual(get_rate_map(self.ws), {"USD": Decimal("1")})

    def test_rate_map_includes_configured_rates(self):
        ExchangeRate.objects.create(workspace=self.ws, currency="EUR", rate_to_base=Decimal("1.08"))
        rate_map = get_rate_map(self.ws)
        self.assertEqual(rate_map["USD"], Decimal("1"))
        self.assertEqual(rate_map["EUR"], Decimal("1.08"))

    def test_convert_uses_rate(self):
        rate_map = {"USD": Decimal("1"), "EUR": Decimal("1.08")}
        self.assertEqual(convert(Decimal("10"), "EUR", rate_map), Decimal("10.80"))
        self.assertEqual(convert(Decimal("10"), "USD", rate_map), Decimal("10"))

    def test_convert_returns_none_without_rate(self):
        rate_map = {"USD": Decimal("1")}
        self.assertIsNone(convert(Decimal("10"), "GBP", rate_map))


class ExchangeRateApiTests(APITestCase):
    URL = "/api/v1/exchange-rates/"

    def setUp(self):
        self.owner = User.objects.create_user("alice", "a@example.com", "pw")
        self.member = User.objects.create_user("bob", "b@example.com", "pw")
        self.ws = make_workspace(self.owner)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)

    def _headers(self):
        return {HEADER: str(self.ws.id)}

    def test_member_can_create_rate(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post(
            self.URL, {"currency": "eur", "rate_to_base": "1.08"}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["currency"], "EUR")  # se normaliza a mayúsculas

    def test_posting_same_currency_upserts(self):
        self.client.force_authenticate(self.owner)
        self.client.post(self.URL, {"currency": "EUR", "rate_to_base": "1.00"}, **self._headers())
        resp = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "1.08"}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(ExchangeRate.objects.count(), 1)
        self.assertEqual(ExchangeRate.objects.get().rate_to_base, Decimal("1.08"))

    def test_cannot_set_rate_for_base_currency(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            self.URL, {"currency": "USD", "rate_to_base": "1"}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_bad_currency_code(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            self.URL, {"currency": "EU1", "rate_to_base": "1"}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_non_positive_rate(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "0"}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_then_recreate_same_currency_works(self):
        self.client.force_authenticate(self.owner)
        created = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "1.08"}, **self._headers()
        )
        rate_id = created.data["id"]
        delete_resp = self.client.delete(f"{self.URL}{rate_id}/", **self._headers())
        self.assertEqual(delete_resp.status_code, status.HTTP_204_NO_CONTENT)

        recreate = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "1.10"}, **self._headers()
        )
        self.assertEqual(recreate.status_code, status.HTTP_201_CREATED, recreate.data)


class WorkspaceBaseCurrencyApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("alice", "a@example.com", "pw")
        self.member = User.objects.create_user("bob", "b@example.com", "pw")
        self.ws = make_workspace(self.owner)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)

    def test_owner_can_change_base_currency(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.patch(f"/api/v1/workspaces/{self.ws.id}/", {"base_currency": "EUR"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["base_currency"], "EUR")

    def test_member_cannot_change_base_currency(self):
        self.client.force_authenticate(self.member)
        resp = self.client.patch(f"/api/v1/workspaces/{self.ws.id}/", {"base_currency": "EUR"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TransactionCurrencyDerivationTests(APITestCase):
    """`Transaction.currency` siempre sale de `wallet.currency`, nunca del cliente."""

    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.ws = make_workspace(self.user)
        self.eur_wallet = Wallet.objects.create(
            workspace=self.ws, name="Cuenta EUR", purpose=Wallet.PURPOSE_SPENDING, currency="EUR"
        )
        self.category = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def test_currency_derived_on_create(self):
        txn = Transaction.objects.create(
            wallet=self.eur_wallet, category=self.category, amount=Decimal("10"),
            date=timezone.localdate(), type=Transaction.TYPE_EXPENSE,
            currency="USD",  # se ignora -- gana la de la cartera
        )
        self.assertEqual(txn.currency, "EUR")

    def test_api_ignores_client_provided_currency(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})
        resp = self.client.post(
            "/api/v1/transactions/",
            {
                "wallet": str(self.eur_wallet.id),
                "category": str(self.category.id),
                "amount": "10",
                "date": str(timezone.localdate()),
                "currency": "JPY",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["currency"], "EUR")


class CurrencyAwareReportsTests(APITestCase):
    """Workspace con carteras en dos monedas: sin tasa, la extranjera se
    excluye de los totales; con tasa, se convierte."""

    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.ws = make_workspace(self.user, base_currency="USD")
        self.usd_wallet = Wallet.objects.create(
            workspace=self.ws, name="USD", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("100.00"),
        )
        self.eur_wallet = Wallet.objects.create(
            workspace=self.ws, name="EUR", purpose=Wallet.PURPOSE_SPENDING,
            currency="EUR", opening_balance=Decimal("100.00"),
        )
        self.category = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.today = timezone.localdate()
        Transaction.objects.create(
            wallet=self.usd_wallet, category=self.category, amount=Decimal("20"),
            date=self.today, type=Transaction.TYPE_EXPENSE,
        )
        Transaction.objects.create(
            wallet=self.eur_wallet, category=self.category, amount=Decimal("30"),
            date=self.today, type=Transaction.TYPE_EXPENSE,
        )

    def test_net_worth_excludes_currency_without_rate(self):
        data = net_worth_breakdown(self.ws)
        self.assertEqual(data["base_currency"], "USD")
        # Sólo la cartera USD (100 - 20 = 80) cuenta; la de EUR queda afuera.
        self.assertEqual(data["net"], Decimal("80.00"))

    def test_net_worth_includes_converted_amount_once_rate_exists(self):
        ExchangeRate.objects.create(workspace=self.ws, currency="EUR", rate_to_base=Decimal("2"))
        data = net_worth_breakdown(self.ws)
        # USD: 100-20=80. EUR: (100-30)*2=140. Total 220.
        self.assertEqual(data["net"], Decimal("220.00"))

    def test_spending_by_category_excludes_currency_without_rate(self):
        rows = spending_by_category(self.ws, self.user, self.today.year, self.today.month)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spent"], Decimal("20"))

    def test_spending_by_category_converts_with_rate(self):
        ExchangeRate.objects.create(workspace=self.ws, currency="EUR", rate_to_base=Decimal("2"))
        rows = spending_by_category(self.ws, self.user, self.today.year, self.today.month)
        # 20 (USD) + 30*2 (EUR) = 80
        self.assertEqual(rows[0]["spent"], Decimal("80"))

    def test_budget_vs_actual_converts_spent(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.category, amount=Decimal("100"),
            month=self.today.month, year=self.today.year,
        )
        ExchangeRate.objects.create(workspace=self.ws, currency="EUR", rate_to_base=Decimal("2"))
        report = budget_vs_actual(self.ws, self.user, self.today.year, self.today.month)
        self.assertEqual(report["base_currency"], "USD")
        row = report["rows"][0]
        self.assertEqual(row["spent"], Decimal("80"))

    def test_monthly_cashflow_converts_expenses(self):
        ExchangeRate.objects.create(workspace=self.ws, currency="EUR", rate_to_base=Decimal("2"))
        series = monthly_cashflow(self.ws, self.user, months=1)
        self.assertEqual(series[0]["expenses"], Decimal("80"))
