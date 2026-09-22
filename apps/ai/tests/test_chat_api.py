"""`POST /api/v1/ai/chat/` — el borde del endpoint de chat.

Mismo criterio que recibos, texto y voz: validación de entrada, que un
Gemini caído no sea un 500 nuestro, y que la cuota corte antes de gastar la
llamada. La lógica de qué función se llama y cómo se arma la respuesta ya se
prueba en `test_chat.py`.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.ai import chat, models as m
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

URL = "/api/v1/ai/chat/"
HEADER = "HTTP_X_WORKSPACE_ID"
_GENERATE = "apps.ai.chat.generate"


def _select_none():
    return GeminiResponse(
        text=json.dumps({"function": "none", "none_reason": "No tiene que ver con tus finanzas."}),
        model="gemini-3.5-flash-lite", input_tokens=300, output_tokens=40, latency_ms=200,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ChatApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 1},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _h(self):
        return {HEADER: str(self.ws.id)}

    def test_devuelve_la_respuesta(self):
        with patch(_GENERATE, return_value=_select_none()):
            resp = self.client.post(URL, {"question": "¿en qué invierto?"}, **self._h())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["answer"], "No tiene que ver con tus finanzas.")
        self.assertIsNone(resp.data["function_used"])

    def test_registra_el_consumo_como_chat(self):
        with patch(_GENERATE, return_value=_select_none()):
            self.client.post(URL, {"question": "algo"}, **self._h())
        self.assertEqual(m.AIUsage.objects.filter(operation=m.OP_CHAT).count(), 1)

    def test_sin_pregunta_es_400_y_no_gasta_una_llamada(self):
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {"question": "   "}, **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_una_pregunta_larguisima_se_rechaza_antes_de_gastar_una_llamada(self):
        with patch(_GENERATE) as generate:
            resp = self.client.post(
                URL, {"question": "x" * (chat.MAX_QUESTION_LENGTH + 1)}, **self._h()
            )
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pide_autenticacion(self):
        self.client.force_authenticate(None)
        resp = self.client.post(URL, {"question": "algo"}, **self._h())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_exige_el_header_de_workspace(self):
        resp = self.client.post(URL, {"question": "algo"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gemini_caido_es_503_y_no_un_500_nuestro(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            resp = self.client.post(URL, {"question": "algo"}, **self._h())
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_al_agotar_la_cuota_del_plan_responde_429(self):
        with patch(_GENERATE, return_value=_select_none()):
            resp = self.client.post(URL, {"question": "algo"}, **self._h())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {"question": "otra"}, **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
