"""
`services.run()` — el camino único por el que el resto del proyecto va a usar
IA. Lo que se prueba es el orden de las cosas: cuota antes de gastar plata, y
registro de todo lo que sí se gastó.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.ai import models as m
from apps.ai import quotas, services
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.common.models import ModuleFlag
from apps.workspaces.models import Workspace

User = get_user_model()

_GENERATE = "apps.ai.services.generate"


def _ok(model="gemini-2.5-flash", tokens=(1500, 300)):
    return GeminiResponse(
        text='{"amount": 12.5}', model=model,
        input_tokens=tokens[0], output_tokens=tokens[1], latency_ms=812,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class RunTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 2, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")

    def _run(self, operation=m.OP_RECEIPT):
        return services.run(
            user=self.user, operation=operation,
            parts=[{"text": "hola"}], workspace=self.workspace,
        )

    def test_registra_el_consumo_con_tokens_costo_y_latencia(self):
        with patch(_GENERATE, return_value=_ok()):
            self._run()
        row = m.AIUsage.objects.get()
        self.assertEqual(row.operation, m.OP_RECEIPT)
        self.assertEqual((row.input_tokens, row.output_tokens), (1500, 300))
        self.assertEqual(row.latency_ms, 812)
        self.assertEqual(row.workspace, self.workspace)
        self.assertTrue(row.counts_against_quota)
        # 1500 in × $0.30/Mtok + 300 out × $2.50/Mtok = $0.00120
        self.assertEqual(row.cost_micros, 1200)

    def test_no_llama_a_gemini_si_ya_no_queda_cuota(self):
        """El chequeo va antes de gastar la llamada, no después."""
        with patch(_GENERATE, return_value=_ok()) as generate:
            self._run()
            self._run()
            with self.assertRaises(quotas.QuotaExceeded):
                self._run()
            self.assertEqual(generate.call_count, 2)
        # La rechazada no deja fila: no llegó a costar nada.
        self.assertEqual(m.AIUsage.objects.count(), 2)

    def test_una_falla_de_gemini_se_registra_pero_no_le_cuesta_la_cuota_al_usuario(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            with self.assertRaises(AIUnavailable):
                self._run()
        row = m.AIUsage.objects.get()
        self.assertEqual(row.status, m.STATUS_ERROR)
        self.assertEqual(row.error_code, "timeout")
        self.assertFalse(row.counts_against_quota)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 0)

    def test_el_resumen_mensual_se_registra_pero_no_gasta_cuota(self):
        with patch(_GENERATE, return_value=_ok()):
            services.run(user=self.user, operation=m.OP_SUMMARY, parts=[{"text": "hola"}])
        row = m.AIUsage.objects.get()
        self.assertFalse(row.counts_against_quota)

    def test_cada_operacion_va_al_modelo_que_le_toca(self):
        with patch(_GENERATE, return_value=_ok()) as generate:
            services.run(user=self.user, operation=m.OP_PARSE, parts=[{"text": "hola"}])
        self.assertEqual(generate.call_args[1]["model"], "gemini-2.5-flash-lite")

    def test_el_audio_va_al_modelo_que_lo_acepta_y_se_cobra_mas_caro(self):
        """Flash-Lite no acepta audio, y Gemini cobra el audio de entrada a
        $1.00/Mtok contra $0.30 del texto."""
        with patch(_GENERATE, return_value=_ok(tokens=(800, 120))) as generate:
            services.run(
                user=self.user, operation=m.OP_PARSE,
                parts=[{"text": "hola"}], has_audio=True,
            )
        self.assertEqual(generate.call_args[1]["model"], "gemini-2.5-flash")
        # 800 × $1.00/Mtok + 120 × $2.50/Mtok = $0.0011
        self.assertEqual(m.AIUsage.objects.get().cost_micros, 1100)


@override_settings(GEMINI_API_KEY="")
class SinKeyTests(TestCase):
    def setUp(self):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def test_availability_dice_que_no_esta_habilitada(self):
        data = services.availability_for(self.user)
        self.assertFalse(data["enabled"])
        # Las cuotas se informan igual: sirven para la pantalla de planes.
        self.assertEqual(data["quotas"][m.OP_RECEIPT]["limit"], 3)


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ModuleFlagTests(TestCase):
    """El interruptor manual `ModuleFlag("ai")` apaga la IA aparte de si hay
    `GEMINI_API_KEY` -- ver `services.availability_for`."""

    def setUp(self):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def test_key_present_but_module_flag_off(self):
        # `update_or_create` porque `common.0002_seed_module_flags` ya
        # sembró la fila "ai" (habilitada) -- ver esa migración.
        ModuleFlag.objects.update_or_create(key="ai", defaults={"label": "IA", "is_enabled": False})
        self.assertFalse(services.availability_for(self.user)["enabled"])

    def test_key_present_and_no_flag_row_is_enabled(self):
        # Fail-open: sin fila para esta clave, el interruptor no bloquea
        # nada -- se borra la que sembró la migración para probar el caso
        # real de "sin fila todavía".
        ModuleFlag.objects.filter(key="ai").delete()
        self.assertTrue(services.availability_for(self.user)["enabled"])

    def test_key_present_and_flag_explicitly_on(self):
        ModuleFlag.objects.update_or_create(key="ai", defaults={"label": "IA", "is_enabled": True})
        self.assertTrue(services.availability_for(self.user)["enabled"])

    def test_run_se_niega_con_el_modulo_apagado_y_no_deja_rastro(self):
        ModuleFlag.objects.update_or_create(key="ai", defaults={"label": "IA", "is_enabled": False})
        with patch(_GENERATE) as generate:
            with self.assertRaises(AIUnavailable) as ctx:
                services.run(user=self.user, operation=m.OP_PARSE, parts=[{"text": "hola"}])
        self.assertEqual(ctx.exception.code, "module_disabled")
        generate.assert_not_called()
        self.assertEqual(m.AIUsage.objects.count(), 0)
