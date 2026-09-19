"""`POST /api/v1/ai/parse/` — la frase suelta.

Mismo borde que el de recibos: validación de entrada, que no se cree nada, que
un Gemini caído no sea un 500 nuestro, y que la cuota corte antes de gastar la
llamada.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.ai import models as m
from apps.ai import parsing
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

URL = "/api/v1/ai/parse/"
HEADER = "HTTP_X_WORKSPACE_ID"
# Igual que en el de recibos: se parchea el cliente de Gemini y no
# `services.run`, para que la cuota y el registro corran de verdad.
_GENERATE = "apps.ai.services.generate"

_CRUDO = {
    "type": "expense",
    "amount": "12.50",
    "currency": "USD",
    "date": "2026-09-18",
    "merchant": "Super Selectos",
    "note": "almuerzo",
    "wallet_hint": "Tarjeta",
    "category_hint": "Comida fuera",
    "confidence": {"amount": "high", "date": "medium", "merchant": "high"},
}


def _respuesta():
    return GeminiResponse(
        text=json.dumps(_CRUDO), model="gemini-2.5-flash-lite",
        input_tokens=630, output_tokens=120, latency_ms=410,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ParseTextTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 2, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        grupo = Category.objects.create(
            workspace=cls.ws, name="Alimentación", type=Category.TYPE_EXPENSE
        )
        Category.objects.create(
            workspace=cls.ws, name="Comida fuera", type=Category.TYPE_EXPENSE, parent=grupo,
        )
        cls.tarjeta = Wallet.objects.create(workspace=cls.ws, name="Tarjeta", currency="USD")

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _h(self):
        return {HEADER: str(self.ws.id)}

    def _post(self, **data):
        with patch(_GENERATE, return_value=_respuesta()):
            return self.client.post(
                URL, {"text": "gasté 12.50 en almuerzo con la tarjeta", **data}, **self._h()
            )

    def test_devuelve_la_candidata(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(resp.data["amount"]), Decimal("12.50"))
        self.assertEqual(resp.data["type"], "expense")
        self.assertEqual(resp.data["wallet"], str(self.tarjeta.id))
        self.assertEqual(resp.data["wallet_source"], "text")
        self.assertEqual(resp.data["confidence"]["date"], "medium")

    def test_no_crea_la_transaccion(self):
        self._post()
        self.assertEqual(Transaction.objects.count(), 0)

    def test_registra_el_consumo_como_parseo(self):
        self._post()
        self.assertEqual(m.AIUsage.objects.filter(operation=m.OP_PARSE).count(), 1)

    def test_sin_texto_es_400_y_no_gasta_una_llamada(self):
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {"text": "   "}, **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_texto_larguisimo_se_rechaza_antes_de_gastar_una_llamada(self):
        with patch(_GENERATE) as generate:
            resp = self.client.post(
                URL, {"text": "x" * (parsing.MAX_TEXT_LENGTH + 1)}, **self._h()
            )
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pide_autenticacion(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.post(URL, {"text": "algo"}, **self._h()).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_exige_el_header_de_workspace(self):
        self.assertEqual(
            self.client.post(URL, {"text": "algo"}).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_una_cartera_de_otro_workspace_no_deja_ver_sus_transacciones(self):
        ajeno = Workspace.objects.create(name="Ajeno")
        cartera_ajena = Wallet.objects.create(workspace=ajeno, name="Otra", currency="USD")
        self.assertEqual(
            self._post(wallet=str(cartera_ajena.id)).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_gemini_caido_es_503_y_no_un_500_nuestro(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            resp = self.client.post(URL, {"text": "algo"}, **self._h())
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_al_agotar_la_cuota_del_plan_responde_429(self):
        for _ in range(2):
            self.assertEqual(self._post().status_code, status.HTTP_200_OK)
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {"text": "algo"}, **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_la_cuota_de_parseos_es_distinta_de_la_de_recibos(self):
        """Comparten la base de IA pero no el tope: gastar los parseos no debe
        dejar a nadie sin escanear recibos."""
        for _ in range(2):
            self._post()
        resp = self.client.get("/api/v1/ai/status/")
        self.assertEqual(resp.data["quotas"]["parse"]["remaining"], 0)
        self.assertEqual(resp.data["quotas"]["receipt"]["remaining"], 3)
