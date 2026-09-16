"""Interés editable en carteras de ahorro y proyección de retorno estimado
por mes (saldo diario ponderado) -- ver `services.savings_interest_projection`."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.accounts.services import savings_interest_projection
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class SavingsInterestProjectionServiceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.income_cat = Category.objects.create(
            workspace=cls.ws, name="Aporte", type=Category.TYPE_INCOME
        )

    def _savings_wallet(self, **kwargs):
        return Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS, **kwargs
        )

    def test_raises_for_non_savings_wallet(self):
        w = Wallet.objects.create(workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING)
        with self.assertRaises(ValueError):
            savings_interest_projection(w, 2026, 3)

    @patch("apps.accounts.services.timezone.localdate")
    def test_no_rate_configured_yields_zero_interest(self, mock_localdate):
        mock_localdate.return_value = date(2026, 4, 1)
        w = self._savings_wallet(opening_balance=Decimal("1000.00"))
        data = savings_interest_projection(w, 2026, 3)
        self.assertEqual(data["estimated_interest"], Decimal("0.00"))

    @patch("apps.accounts.services.timezone.localdate")
    def test_flat_balance_monthly_compounding_is_simple_daily_interest(self, mock_localdate):
        # Marzo ya terminó por completo -- sin días futuros que congelar.
        mock_localdate.return_value = date(2026, 4, 1)
        w = self._savings_wallet(
            opening_balance=Decimal("3000.00"),
            savings_interest_rate=Decimal("3.650"),
            savings_interest_rate_period=Wallet.INTEREST_PERIOD_ANNUAL,
            savings_interest_compounding=Wallet.COMPOUNDING_MONTHLY,
        )
        data = savings_interest_projection(w, 2026, 3)
        # daily_rate = 3.65% / 365 = 0.0001 exacto; 31 días de marzo sin
        # movimientos -- capitalización mensual no llega a componer dentro
        # del mismo mes (el único corte cae el último día).
        self.assertEqual(data["opening_balance"], Decimal("3000.00"))
        self.assertEqual(data["closing_balance"], Decimal("3000.00"))
        self.assertEqual(data["estimated_interest"], Decimal("9.30"))  # 3000*0.0001*31
        self.assertFalse(data["is_partial_month"])

    @patch("apps.accounts.services.timezone.localdate")
    def test_monthly_rate_is_converted_to_annual(self, mock_localdate):
        mock_localdate.return_value = date(2026, 4, 1)
        w_annual = self._savings_wallet(
            opening_balance=Decimal("3000.00"),
            savings_interest_rate=Decimal("3.650"),
            savings_interest_rate_period=Wallet.INTEREST_PERIOD_ANNUAL,
        )
        w_monthly = self._savings_wallet(
            opening_balance=Decimal("3000.00"),
            savings_interest_rate=(Decimal("3.650") / 12),
            savings_interest_rate_period=Wallet.INTEREST_PERIOD_MONTHLY,
        )
        data_annual = savings_interest_projection(w_annual, 2026, 3)
        data_monthly = savings_interest_projection(w_monthly, 2026, 3)
        self.assertEqual(data_annual["estimated_interest"], data_monthly["estimated_interest"])

    @patch("apps.accounts.services.timezone.localdate")
    def test_daily_compounding_earns_more_than_monthly_for_same_rate(self, mock_localdate):
        mock_localdate.return_value = date(2026, 4, 1)
        w_daily = self._savings_wallet(
            opening_balance=Decimal("100000.00"),
            savings_interest_rate=Decimal("36.500"),
            savings_interest_compounding=Wallet.COMPOUNDING_DAILY,
        )
        w_monthly = self._savings_wallet(
            opening_balance=Decimal("100000.00"),
            savings_interest_rate=Decimal("36.500"),
            savings_interest_compounding=Wallet.COMPOUNDING_MONTHLY,
        )
        daily = savings_interest_projection(w_daily, 2026, 3)["estimated_interest"]
        monthly = savings_interest_projection(w_monthly, 2026, 3)["estimated_interest"]
        self.assertGreater(daily, monthly)

    @patch("apps.accounts.services.timezone.localdate")
    def test_partial_month_freezes_future_days_at_last_known_balance(self, mock_localdate):
        mock_localdate.return_value = date(2026, 3, 10)
        w = self._savings_wallet(
            opening_balance=Decimal("1000.00"),
            savings_interest_rate=Decimal("3.650"),
            savings_interest_compounding=Wallet.COMPOUNDING_MONTHLY,
        )
        Transaction.objects.create(
            wallet=w, category=self.income_cat, amount=Decimal("500.00"), date=date(2026, 3, 5)
        )
        w.refresh_from_db()
        data = savings_interest_projection(w, 2026, 3)
        self.assertTrue(data["is_partial_month"])
        # Días 1-4 a 1000, días 5-31 a 1500 (el depósito ya ocurrió antes de
        # "hoy" -- los días después del 10 se congelan en el último saldo
        # real conocido, no se inventan más movimientos).
        self.assertEqual(data["closing_balance"], Decimal("1500.00"))
        self.assertEqual(data["estimated_interest"], Decimal("4.45"))


class SavingsInterestApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def _savings_wallet(self, **kwargs):
        return Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS, **kwargs
        )

    def test_endpoint_shape(self):
        w = self._savings_wallet(
            opening_balance=Decimal("3000.00"), savings_interest_rate=Decimal("3.650")
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/interest-projection/?year=2026&month=3")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["estimated_interest"], "9.30")
        self.assertEqual(resp.data["compounding"], "monthly")

    def test_404_for_non_savings_wallet(self):
        w = Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING
        )
        resp = self.client.get(f"/api/v1/wallets/{w.id}/interest-projection/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_404_without_rate_configured(self):
        w = self._savings_wallet()
        resp = self.client.get(f"/api/v1/wallets/{w.id}/interest-projection/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_400_for_invalid_month(self):
        w = self._savings_wallet(savings_interest_rate=Decimal("3.650"))
        resp = self.client.get(f"/api/v1/wallets/{w.id}/interest-projection/?month=13")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_defaults_to_current_month(self):
        w = self._savings_wallet(savings_interest_rate=Decimal("3.650"))
        resp = self.client.get(f"/api/v1/wallets/{w.id}/interest-projection/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_rate_rejected_on_non_savings_wallet(self):
        resp = self.client.post(
            "/api/v1/wallets/",
            {"name": "Banco", "purpose": Wallet.PURPOSE_SPENDING, "savings_interest_rate": "3.5"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("savings_interest_rate", resp.data)

    def test_rate_accepted_on_savings_wallet(self):
        resp = self.client.post(
            "/api/v1/wallets/",
            {
                "name": "Ahorro",
                "purpose": Wallet.PURPOSE_SAVINGS,
                "savings_interest_rate": "3.5",
                "savings_interest_rate_period": Wallet.INTEREST_PERIOD_ANNUAL,
                "savings_interest_compounding": Wallet.COMPOUNDING_DAILY,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["savings_interest_rate"], "3.500")
