"""
Parseo de texto libre (`apps/ai/parsing.py`).

Nunca se llama a Gemini. Lo que se prueba es lo de siempre con IA: que el
resultado se normalice antes de creerle, y que el modelo no pueda devolver una
cartera o una categoría que el usuario no podía elegir.
"""
import datetime as dt
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.ai import models as m_ai
from apps.ai import parsing
from apps.ai.client import GeminiResponse
from apps.billing.models import Plan
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

_RUN = "apps.ai.parsing.run"

_CRUDO = {
    "type": "expense",
    "amount": "12.50",
    "currency": "USD",
    "date": "2026-09-18",
    "merchant": "Super Selectos",
    "note": "almuerzo",
    "wallet_hint": "Tarjeta",
    "category_hint": "Comida fuera",
    "confidence": {"amount": "high", "date": "high", "merchant": "medium"},
}


def _respuesta(payload):
    return GeminiResponse(
        text=json.dumps(payload), model="gemini-2.5-flash-lite",
        input_tokens=630, output_tokens=120, latency_ms=410,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ParseTestBase(TestCase):
    """Fixtures y el helper `_parse`. Sin tests propios: si los tuviera, cada
    subclase los volvería a correr."""

    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 50, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.otro = User.objects.create_user("beto", "beto@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.grupo = Category.objects.create(
            workspace=cls.ws, name="Alimentación", type=Category.TYPE_EXPENSE
        )
        cls.comida = Category.objects.create(
            workspace=cls.ws, name="Comida fuera", type=Category.TYPE_EXPENSE, parent=cls.grupo,
        )
        cls.efectivo = Wallet.objects.create(workspace=cls.ws, name="Efectivo", currency="USD")
        cls.tarjeta = Wallet.objects.create(workspace=cls.ws, name="Tarjeta", currency="USD")

    def _parse(self, crudo=None, **kwargs):
        with patch(_RUN, return_value=_respuesta(crudo if crudo is not None else _CRUDO)):
            return parsing.parse(
                user=self.user, workspace=self.ws, text="gasté 12.50 en almuerzo", **kwargs
            )


class ParseoTests(ParseTestBase):
    def test_devuelve_la_candidata_completa(self):
        c = self._parse()
        self.assertEqual(c["type"], "expense")
        self.assertEqual(c["amount"], Decimal("12.50"))
        self.assertEqual(c["date"], dt.date(2026, 9, 18))
        self.assertEqual(c["description"], "almuerzo")
        self.assertEqual(c["wallet"], self.tarjeta.id)
        self.assertEqual(c["category"], self.comida.id)

    def test_me_pagaron_es_un_ingreso(self):
        c = self._parse({**_CRUDO, "type": "income"})
        self.assertEqual(c["type"], "income")

    def test_un_tipo_que_no_existe_cae_a_gasto(self):
        """La inmensa mayoría de lo que la gente anota son gastos, así que es
        el default menos sorprendente."""
        c = self._parse({**_CRUDO, "type": "transferencia"})
        self.assertEqual(c["type"], "expense")

    def test_sin_nota_se_usa_el_comercio(self):
        c = self._parse({**_CRUDO, "note": ""})
        self.assertEqual(c["description"], "Super Selectos")

    def test_le_pasa_al_modelo_la_fecha_de_hoy_y_los_nombres_reales(self):
        """Sin los nombres, "con la tarjeta" vuelve como texto libre que hay
        que adivinar; con ellos el modelo elige de una lista cerrada."""
        with patch(_RUN, return_value=_respuesta(_CRUDO)) as run:
            parsing.parse(user=self.user, workspace=self.ws, text="algo")
        prompt = run.call_args[1]["system_instruction"]
        self.assertIn(timezone.localdate().isoformat(), prompt)
        self.assertIn("- Tarjeta", prompt)
        self.assertIn("- Comida fuera", prompt)

    def test_la_frase_se_corta_antes_de_mandarla(self):
        """Un correo entero pegado no es una transacción, y cortarlo acá acota
        lo que se gasta en tokens."""
        with patch(_RUN, return_value=_respuesta(_CRUDO)) as run:
            parsing.parse(user=self.user, workspace=self.ws, text="x" * 5000)
        self.assertEqual(len(run.call_args[1]["parts"][0]["text"]), parsing.MAX_TEXT_LENGTH)


class NormalizacionTests(ParseTestBase):
    def test_un_monto_que_no_se_dijo_queda_vacio_y_marcado(self):
        c = self._parse({**_CRUDO, "amount": ""})
        self.assertIsNone(c["amount"])
        self.assertEqual(c["confidence"]["amount"], "low")

    def test_una_fecha_futura_cae_a_hoy(self):
        futuro = (timezone.localdate() + dt.timedelta(days=5)).isoformat()
        self.assertEqual(self._parse({**_CRUDO, "date": futuro})["date"], timezone.localdate())

    def test_sin_fecha_en_la_frase_queda_hoy_y_marcada(self):
        c = self._parse({**_CRUDO, "date": ""})
        self.assertEqual(c["date"], timezone.localdate())
        self.assertEqual(c["confidence"]["date"], "low")

    def test_una_moneda_que_no_es_iso_se_descarta(self):
        self.assertIsNone(self._parse({**_CRUDO, "currency": "pisto"})["currency"])


class CarteraTests(ParseTestBase):
    def test_la_cartera_que_nombra_la_frase(self):
        c = self._parse()
        self.assertEqual(c["wallet"], self.tarjeta.id)
        self.assertEqual(c["wallet_source"], "text")

    def test_si_la_frase_no_nombra_ninguna_el_cliente_se_queda_con_la_suya(self):
        c = self._parse({**_CRUDO, "wallet_hint": ""})
        self.assertIsNone(c["wallet"])
        self.assertIsNone(c["wallet_source"])

    def test_un_nombre_inventado_por_el_modelo_no_resuelve_nada(self):
        c = self._parse({**_CRUDO, "wallet_hint": "Cuenta suiza"})
        self.assertIsNone(c["wallet"])

    def test_una_cartera_privada_ajena_no_entra_al_prompt_ni_al_resultado(self):
        """Que el modelo la viera ya sería filtrarla, aunque no la devolviera."""
        privada = Wallet.objects.create(
            workspace=self.ws, name="Secreta", currency="USD",
            visibility=Wallet.VISIBILITY_PRIVATE, owner=self.otro,
        )
        with patch(_RUN, return_value=_respuesta({**_CRUDO, "wallet_hint": "Secreta"})) as run:
            c = parsing.parse(user=self.user, workspace=self.ws, text="algo")
        self.assertNotIn("Secreta", run.call_args[1]["system_instruction"])
        self.assertIsNone(c["wallet"])
        self.assertNotEqual(c["wallet"], privada.id)


class CategoriaTests(ParseTestBase):
    def test_el_historial_gana_sobre_la_sugerencia_del_modelo(self):
        otra = Category.objects.create(
            workspace=self.ws, name="Supermercado", type=Category.TYPE_EXPENSE, parent=self.grupo,
        )
        for _ in range(2):
            Transaction.objects.create(
                wallet=self.efectivo, type=Transaction.TYPE_EXPENSE, category=otra,
                amount=Decimal("5.00"), date=timezone.localdate(), description="Super Selectos",
            )
        c = self._parse()
        self.assertEqual(c["category"], otra.id)
        self.assertEqual(c["category_source"], "history")

    def test_sin_historial_se_usa_la_del_modelo(self):
        c = self._parse()
        self.assertEqual(c["category"], self.comida.id)
        self.assertEqual(c["category_source"], "ai")

    def test_una_categoria_del_tipo_equivocado_no_se_usa(self):
        """Una categoría de gasto en un ingreso daría una transacción que el
        serializer rechaza."""
        c = self._parse({**_CRUDO, "type": "income"})
        self.assertIsNone(c["category"])

    def test_un_nombre_inventado_por_el_modelo_no_resuelve_nada(self):
        c = self._parse({**_CRUDO, "category_hint": "Criptomonedas"})
        self.assertIsNone(c["category"])


class DuplicadosTests(ParseTestBase):
    def _con_transaccion(self, wallet):
        return Transaction.objects.create(
            wallet=wallet, type=Transaction.TYPE_EXPENSE, category=self.comida,
            amount=Decimal("12.50"), date=dt.date(2026, 9, 18), description="almuerzo",
        )

    def test_busca_en_la_cartera_que_nombro_la_frase(self):
        ya = self._con_transaccion(self.tarjeta)
        self.assertEqual([d["id"] for d in self._parse()["possible_duplicates"]], [ya.id])

    def test_si_la_frase_no_nombra_cartera_usa_la_que_trae_el_cliente(self):
        ya = self._con_transaccion(self.efectivo)
        c = self._parse({**_CRUDO, "wallet_hint": ""}, wallet=self.efectivo)
        self.assertEqual([d["id"] for d in c["possible_duplicates"]], [ya.id])

    def test_sin_cartera_de_ningun_lado_no_hay_contra_que_comparar(self):
        self._con_transaccion(self.efectivo)
        self.assertEqual(self._parse({**_CRUDO, "wallet_hint": ""})["possible_duplicates"], [])


class ParseAudioTests(ParseTestBase):
    """`parse_audio` comparte toda la normalización y resolución con `parse`
    (probadas arriba) -- acá sólo lo que le es propio: manda el audio como
    `inline_data`, no como texto, y pasa `has_audio=True` para que `services.
    run` lo cueste con el precio de audio en vez del de texto."""

    def test_manda_el_audio_como_inline_data_y_marca_has_audio(self):
        with patch(_RUN, return_value=_respuesta(_CRUDO)) as run:
            parsing.parse_audio(
                user=self.user, workspace=self.ws,
                audio_bytes=b"un-audio-cualquiera", content_type="audio/aac",
            )
        self.assertTrue(run.call_args[1]["has_audio"])
        parts = run.call_args[1]["parts"]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "audio/aac")
        self.assertEqual(run.call_args[1]["operation"], m_ai.OP_PARSE)

    def test_devuelve_la_misma_candidata_que_el_texto(self):
        with patch(_RUN, return_value=_respuesta(_CRUDO)):
            c = parsing.parse_audio(
                user=self.user, workspace=self.ws,
                audio_bytes=b"un-audio-cualquiera", content_type="audio/aac",
            )
        self.assertEqual(c["amount"], Decimal("12.50"))
        self.assertEqual(c["wallet"], self.tarjeta.id)
        self.assertEqual(c["category"], self.comida.id)

    def test_le_pasa_al_modelo_los_mismos_nombres_reales(self):
        with patch(_RUN, return_value=_respuesta(_CRUDO)) as run:
            parsing.parse_audio(
                user=self.user, workspace=self.ws,
                audio_bytes=b"audio", content_type="audio/aac",
            )
        prompt = run.call_args[1]["system_instruction"]
        self.assertIn("- Tarjeta", prompt)
        self.assertIn("- Comida fuera", prompt)
