"""Endpoint de solo lectura para Villa Wxlter (`GET /api/v1/dashboard/balance/`)."""
from decimal import Decimal

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.workspaces.models import Workspace

URL = "/api/v1/dashboard/balance/"
TOKEN = "test-dashboard-token"


class DashboardBalanceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="Solo", base_currency="USD")
        Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("1234.56"),
        )
        Wallet.objects.create(
            workspace=cls.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS,
            opening_balance=Decimal("100.00"), counts_toward_net_worth=False,
        )

    def test_missing_token_configured_rejects_everything(self):
        # DASHBOARD_API_TOKEN vacío por default en settings de test.
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(DASHBOARD_API_TOKEN=TOKEN)
    def test_no_authorization_header_rejected(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(DASHBOARD_API_TOKEN=TOKEN)
    def test_wrong_token_rejected(self):
        response = self.client.get(URL, HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(DASHBOARD_API_TOKEN=TOKEN)
    def test_correct_token_returns_net_worth_of_the_only_workspace(self):
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 1234.56 (spending, cuenta) — el ahorro no cuenta hacia el neto.
        # DecimalField serializa a string, como el resto del API (ver _Money() en api.py).
        self.assertEqual(response.data["balance"], "1234.56")
        self.assertEqual(response.data["currency"], "USD")

    @override_settings(DASHBOARD_API_TOKEN=TOKEN, DASHBOARD_WORKSPACE_ID=None)
    def test_explicit_workspace_id_is_used(self):
        with override_settings(DASHBOARD_WORKSPACE_ID=str(self.ws.id)):
            response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["balance"], "1234.56")

    @override_settings(DASHBOARD_API_TOKEN=TOKEN)
    def test_ambiguous_workspace_without_explicit_id_fails_loud(self):
        Workspace.objects.create(name="Otro", base_currency="USD")
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
