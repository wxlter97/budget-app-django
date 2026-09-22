"""
`summary.generate` -- conecta los patrones de `behavior_insights` (ya
detectados, no acá) en un solo texto. Se prueba con `services.generate`
mockeado, igual que el resto de `apps.ai` (ver `test_parsing.py`).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.ai import models as m
from apps.ai import summary
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.workspaces.models import Workspace

User = get_user_model()

_GENERATE = "apps.ai.services.generate"

INSIGHTS = [
    {"dedupe_key": "ws:weekend:2026-W13", "title": "Gastás más los fines de semana", "body": "..."},
    {"dedupe_key": "ws:peak_day:2026-W13", "title": "Tenés un día pico", "body": "..."},
]


@override_settings(GEMINI_API_KEY="k-de-prueba")
class GenerateTests(TestCase):
    def setUp(self):
        Plan.objects.create(code="free", name="Gratis", is_default=True, features={})
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")

    def test_arma_el_texto_a_partir_de_lo_que_devuelve_gemini(self):
        response = GeminiResponse(
            text='{"title": "Tu marzo", "body": "Gastaste más los fines de semana."}',
            model="gemini-3.8-flash", input_tokens=200, output_tokens=40, latency_ms=500,
        )
        with patch(_GENERATE, return_value=response) as generate:
            result = summary.generate(user=self.user, workspace=self.workspace, insights=INSIGHTS)

        self.assertEqual(result, {"title": "Tu marzo", "body": "Gastaste más los fines de semana."})
        # Va al modelo asignado a OP_SUMMARY (ver `pricing.MODEL_FOR_OPERATION`).
        self.assertEqual(generate.call_args[1]["model"], "gemini-3.8-flash")
        # No consume cuota: se registra pero no cuenta contra el usuario.
        self.assertFalse(m.AIUsage.objects.get().counts_against_quota)

    def test_los_titulos_y_cuerpos_de_los_patrones_van_en_el_prompt(self):
        response = GeminiResponse(
            text='{"title": "x", "body": "y"}', model="gemini-3.8-flash",
            input_tokens=1, output_tokens=1, latency_ms=1,
        )
        with patch(_GENERATE, return_value=response) as generate:
            summary.generate(user=self.user, workspace=self.workspace, insights=INSIGHTS)

        prompt_text = generate.call_args[1]["parts"][0]["text"]
        self.assertIn("Gastás más los fines de semana", prompt_text)
        self.assertIn("Tenés un día pico", prompt_text)

    def test_titulo_vacio_cae_al_default(self):
        response = GeminiResponse(
            text='{"title": "", "body": "algo"}', model="gemini-3.8-flash",
            input_tokens=1, output_tokens=1, latency_ms=1,
        )
        with patch(_GENERATE, return_value=response):
            result = summary.generate(user=self.user, workspace=self.workspace, insights=INSIGHTS)
        self.assertEqual(result["title"], "Tu resumen del mes")

    def test_propaga_ai_unavailable_para_que_el_llamador_decida_el_respaldo(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            with self.assertRaises(AIUnavailable):
                summary.generate(user=self.user, workspace=self.workspace, insights=INSIGHTS)
