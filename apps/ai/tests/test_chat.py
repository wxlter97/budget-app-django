"""
`chat.ask` — dos llamadas a Gemini (elegir función, redactar), nunca acceso
directo a la base. Se prueba con `chat.generate` mockeado: lo que importa acá
es el pegamento (qué función se llama con qué argumentos, que las dos
llamadas cuenten como una sola unidad de cuota, que un fallo no cobre nada),
no las funciones de `apps.reports.services`, que ya se prueban aparte.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ai import chat, models as m, quotas
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.common.models import ModuleFlag
from apps.workspaces.models import Workspace

User = get_user_model()

_GENERATE = "apps.ai.chat.generate"


def _select(function="none", args=None, none_reason=""):
    payload = {"function": function}
    if args is not None:
        payload["args"] = args
    if none_reason:
        payload["none_reason"] = none_reason
    return GeminiResponse(
        text=json.dumps(payload), model="gemini-3.5-flash-lite",
        input_tokens=300, output_tokens=40, latency_ms=200,
    )


def _answer(text="Gastaste 45.00 USD en Comida en septiembre 2026."):
    return GeminiResponse(
        text=text, model="gemini-3.5-flash-lite",
        input_tokens=900, output_tokens=60, latency_ms=350,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class AskTests(TestCase):
    def setUp(self):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 5},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")

    def _ask(self, question="¿cuánto gasté en comida?"):
        return chat.ask(user=self.user, workspace=self.workspace, question=question)

    def test_sin_una_funcion_que_sirva_devuelve_el_motivo_del_modelo(self):
        with patch(_GENERATE, return_value=_select("none", none_reason="Eso no tiene que ver con tus finanzas.")):
            result = self._ask("¿en qué acciones invierto?")
        self.assertEqual(result["answer"], "Eso no tiene que ver con tus finanzas.")
        self.assertIsNone(result["function_used"])

    def test_sin_motivo_del_modelo_cae_al_mensaje_por_defecto(self):
        with patch(_GENERATE, return_value=_select("none")):
            result = self._ask()
        self.assertEqual(result["answer"], chat.DECLINE_MESSAGE)

    @patch("apps.ai.chat.reports.spending_by_category")
    def test_llama_a_la_funcion_elegida_y_redacta_con_lo_que_devuelve(self, mock_spending):
        mock_spending.return_value = [{"category": "c1", "category_name": "Comida", "spent": Decimal("45.00")}]
        with patch(
            _GENERATE,
            side_effect=[
                _select("spending_by_category", args={"year": "2026", "month": "9"}),
                _answer("Gastaste 45.00 USD en Comida en septiembre 2026."),
            ],
        ):
            result = self._ask()

        mock_spending.assert_called_once_with(self.workspace, self.user, 2026, 9)
        self.assertEqual(result["function_used"], "spending_by_category")
        self.assertEqual(result["answer"], "Gastaste 45.00 USD en Comida en septiembre 2026.")

    @patch("apps.ai.chat.reports.spending_by_category")
    def test_argumentos_invalidos_no_rompen_y_usan_el_default(self, mock_spending):
        mock_spending.return_value = []
        with patch(
            _GENERATE,
            side_effect=[_select("spending_by_category", args={"year": "no-es-un-año"}), _answer()],
        ):
            self._ask()
        today = timezone.localdate()
        mock_spending.assert_called_once_with(self.workspace, self.user, today.year, today.month)

    @patch("apps.ai.chat.reports.monthly_cashflow")
    def test_los_meses_de_cashflow_quedan_acotados_a_24(self, mock_cashflow):
        mock_cashflow.return_value = []
        with patch(_GENERATE, side_effect=[_select("monthly_cashflow", args={"months": "999"}), _answer()]):
            self._ask()
        self.assertEqual(mock_cashflow.call_args[1]["months"], 24)

    @patch("apps.ai.chat.reports.behavior_insights")
    def test_los_insights_no_le_mandan_el_dedupe_key_al_modelo(self, mock_insights):
        mock_insights.return_value = [
            {"dedupe_key": f"{self.workspace.id}:weekend:2026-W10", "title": "T", "body": "B"}
        ]
        with patch(_GENERATE, side_effect=[_select("behavior_insights"), _answer()]) as generate:
            self._ask()
        segunda_llamada = generate.call_args_list[1]
        self.assertNotIn(str(self.workspace.id), segunda_llamada[1]["system_instruction"])
        self.assertIn('"title": "T"', segunda_llamada[1]["system_instruction"])

    @patch("apps.ai.chat.reports.spending_by_category")
    def test_una_sola_fila_de_aiusage_aunque_haya_dos_llamadas_a_gemini(self, mock_spending):
        mock_spending.return_value = []
        with patch(_GENERATE, side_effect=[_select("spending_by_category"), _answer()]) as generate:
            self._ask()
        self.assertEqual(generate.call_count, 2)
        row = m.AIUsage.objects.get()
        self.assertTrue(row.counts_against_quota)
        self.assertEqual(row.operation, m.OP_CHAT)
        # Suma los tokens de las dos llamadas, no sólo la última.
        self.assertEqual(row.input_tokens, 300 + 900)
        self.assertEqual(row.output_tokens, 40 + 60)

    def test_una_pregunta_sin_funcion_tambien_cuenta_una_sola_vez(self):
        with patch(_GENERATE, return_value=_select("none", none_reason="x")):
            self._ask()
        self.assertEqual(m.AIUsage.objects.count(), 1)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_CHAT), 1)

    def test_no_llama_a_gemini_si_ya_no_queda_cuota(self):
        with patch(_GENERATE, return_value=_select("none", none_reason="x")):
            for _ in range(5):
                self._ask()
            with self.assertRaises(quotas.QuotaExceeded):
                self._ask()

    def test_una_falla_en_la_seleccion_no_cobra_cuota(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            with self.assertRaises(AIUnavailable):
                self._ask()
        row = m.AIUsage.objects.get()
        self.assertEqual(row.status, m.STATUS_ERROR)
        self.assertFalse(row.counts_against_quota)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_CHAT), 0)

    @patch("apps.ai.chat.reports.spending_by_category")
    def test_una_falla_en_la_redaccion_tampoco_cobra_cuota(self, mock_spending):
        mock_spending.return_value = []
        with patch(_GENERATE, side_effect=[_select("spending_by_category"), AIUnavailable("timeout")]):
            with self.assertRaises(AIUnavailable):
                self._ask()
        self.assertEqual(m.AIUsage.objects.count(), 1)
        row = m.AIUsage.objects.get()
        self.assertFalse(row.counts_against_quota)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_CHAT), 0)

    def test_con_el_modulo_apagado_no_llama_a_gemini_ni_deja_rastro(self):
        ModuleFlag.objects.update_or_create(key="ai", defaults={"label": "IA", "is_enabled": False})
        with patch(_GENERATE) as generate:
            with self.assertRaises(AIUnavailable) as ctx:
                self._ask()
        self.assertEqual(ctx.exception.code, "module_disabled")
        generate.assert_not_called()
        self.assertEqual(m.AIUsage.objects.count(), 0)

    def test_la_pregunta_se_corta_antes_de_mandarla(self):
        with patch(_GENERATE, return_value=_select("none", none_reason="x")) as generate:
            chat.ask(user=self.user, workspace=self.workspace, question="x" * 5000)
        self.assertEqual(len(generate.call_args[1]["parts"][0]["text"]), chat.MAX_QUESTION_LENGTH)
