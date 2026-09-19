"""
Lectura de recibos (`apps/ai/receipts.py`).

Nunca se llama a Gemini: lo que hay que probar acá es la **normalización** de
lo que el modelo devuelve, que es donde está el riesgo. Un modelo que alucina
un monto o devuelve una fecha del año que viene no debe terminar en el saldo
de nadie, y el usuario tiene que ver marcado lo que no se leyó bien.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.ai import receipts
from apps.ai.client import GeminiResponse
from apps.billing.models import Plan
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

_RUN = "apps.ai.receipts.run"


def _respuesta(payload):
    import json

    return GeminiResponse(
        text=json.dumps(payload), model="gemini-2.5-flash",
        input_tokens=1500, output_tokens=300, latency_ms=900,
    )


_CRUDO = {
    "total": "12.50",
    "tax": "1.44",
    "currency": "USD",
    "date": "2026-09-10",
    "merchant": "Super Selectos",
    "category_hint": "Supermercado",
    "items": [{"description": "Leche", "quantity": "2", "amount": "3.25"}],
    "confidence": {"total": "high", "date": "high", "merchant": "high"},
}


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ReceiptTestBase(TestCase):
    """Fixtures y el helper `_scan`. No lleva tests propios: si los llevara,
    cada subclase los volvería a correr."""

    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 50, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.grupo = Category.objects.create(
            workspace=cls.ws, name="Alimentación", type=Category.TYPE_EXPENSE
        )
        cls.supermercado = Category.objects.create(
            workspace=cls.ws, name="Supermercado", type=Category.TYPE_EXPENSE, parent=cls.grupo,
        )
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Efectivo", currency="USD")

    def _scan(self, crudo=None, **kwargs):
        with patch(_RUN, return_value=_respuesta(crudo if crudo is not None else _CRUDO)):
            return receipts.scan(
                user=self.user, workspace=self.ws,
                file_bytes=b"jpeg-falso", content_type="image/jpeg", **kwargs,
            )


class EscaneoTests(ReceiptTestBase):
    def test_devuelve_los_campos_del_recibo_listos_para_editar(self):
        c = self._scan()
        self.assertEqual(c["amount"], Decimal("12.50"))
        self.assertEqual(c["tax_amount"], Decimal("1.44"))
        self.assertEqual(c["currency"], "USD")
        self.assertEqual(c["date"], dt.date(2026, 9, 10))
        self.assertEqual(c["merchant"], "Super Selectos")
        self.assertEqual(c["items"][0]["description"], "Leche")

    def test_manda_la_imagen_como_inline_data_y_pide_json_estructurado(self):
        with patch(_RUN, return_value=_respuesta(_CRUDO)) as run:
            receipts.scan(
                user=self.user, workspace=self.ws,
                file_bytes=b"jpeg-falso", content_type="image/jpeg",
            )
        kwargs = run.call_args[1]
        self.assertEqual(kwargs["parts"][0]["inline_data"]["mime_type"], "image/jpeg")
        self.assertIsNotNone(kwargs["response_schema"])


class NormalizacionTests(ReceiptTestBase):
    def test_un_monto_que_no_parsea_queda_vacio_y_marcado(self):
        """Mejor un campo vacío que un número inventado: al vacío el usuario lo
        llena, al inventado no lo mira."""
        c = self._scan({**_CRUDO, "total": "no se lee"})
        self.assertIsNone(c["amount"])
        self.assertEqual(c["confidence"]["amount"], "low")

    def test_el_modelo_no_puede_decir_que_esta_seguro_de_un_monto_que_no_se_uso(self):
        c = self._scan({**_CRUDO, "total": "", "confidence": {"total": "high", "date": "high", "merchant": "high"}})
        self.assertEqual(c["confidence"]["amount"], "low")

    def test_limpia_simbolo_de_moneda_y_separador_de_miles(self):
        c = self._scan({**_CRUDO, "total": "$1,234.56"})
        self.assertEqual(c["amount"], Decimal("1234.56"))

    def test_un_monto_negativo_o_absurdo_es_lectura_mala_y_no_un_dato(self):
        self.assertIsNone(self._scan({**_CRUDO, "total": "-12.50"})["amount"])
        self.assertIsNone(self._scan({**_CRUDO, "total": "9999999"})["amount"])

    def test_una_fecha_futura_cae_a_hoy(self):
        futuro = (timezone.localdate() + dt.timedelta(days=30)).isoformat()
        self.assertEqual(self._scan({**_CRUDO, "date": futuro})["date"], timezone.localdate())

    def test_una_fecha_de_hace_anos_suele_ser_un_ano_mal_leido_y_cae_a_hoy(self):
        """Pasa seguido con tickets térmicos gastados: 2019 por 2029."""
        self.assertEqual(self._scan({**_CRUDO, "date": "2019-03-04"})["date"], timezone.localdate())

    def test_una_fecha_ilegible_cae_a_hoy_y_queda_marcada(self):
        c = self._scan({**_CRUDO, "date": ""})
        self.assertEqual(c["date"], timezone.localdate())
        self.assertEqual(c["confidence"]["date"], "low")

    def test_una_moneda_que_no_es_un_codigo_iso_se_descarta(self):
        self.assertIsNone(self._scan({**_CRUDO, "currency": "dólares"})["currency"])

    def test_sin_confianza_en_la_respuesta_todo_queda_en_low(self):
        c = self._scan({"total": "12.50"})
        self.assertEqual(set(c["confidence"].values()), {"low"})

    def test_los_items_sin_descripcion_no_pasan(self):
        c = self._scan({**_CRUDO, "items": [{"description": "  ", "amount": "1"}, "basura"]})
        self.assertEqual(c["items"], [])


class CategoriaTests(ReceiptTestBase):
    def test_el_historial_del_workspace_gana_sobre_la_sugerencia_del_modelo(self):
        """`guess_category_by_merchant` es gratis, determinista y sabe cómo
        categorizó esta gente este comercio antes."""
        otra = Category.objects.create(
            workspace=self.ws, name="Comida fuera", type=Category.TYPE_EXPENSE, parent=self.grupo,
        )
        for _ in range(2):
            Transaction.objects.create(
                wallet=self.wallet, type=Transaction.TYPE_EXPENSE, category=otra,
                amount=Decimal("5.00"), date=timezone.localdate(), description="Super Selectos",
            )
        c = self._scan()
        self.assertEqual(c["category"], otra.id)
        self.assertEqual(c["category_source"], "history")

    def test_sin_historial_se_usa_la_sugerencia_del_modelo(self):
        c = self._scan()
        self.assertEqual(c["category"], self.supermercado.id)
        self.assertEqual(c["category_source"], "ai")

    def test_nunca_sugiere_un_grupo_porque_no_se_puede_guardar_asi(self):
        c = self._scan({**_CRUDO, "category_hint": "Alimentación"})
        self.assertIsNone(c["category"])
        self.assertIsNone(c["category_source"])

    def test_una_categoria_de_otro_workspace_no_se_sugiere(self):
        otra_ws = Workspace.objects.create(name="Ajeno")
        grupo = Category.objects.create(workspace=otra_ws, name="G", type=Category.TYPE_EXPENSE)
        Category.objects.create(
            workspace=otra_ws, name="Ferretería", type=Category.TYPE_EXPENSE, parent=grupo,
        )
        c = self._scan({**_CRUDO, "category_hint": "Ferretería"})
        self.assertIsNone(c["category"])

    def test_sin_sugerencia_ni_historial_la_categoria_queda_para_el_usuario(self):
        c = self._scan({**_CRUDO, "category_hint": ""})
        self.assertIsNone(c["category"])
        self.assertIsNone(c["category_source"])


class DuplicadosTests(ReceiptTestBase):
    def test_avisa_si_ya_hay_una_transaccion_igual_en_esa_cartera(self):
        ya = Transaction.objects.create(
            wallet=self.wallet, type=Transaction.TYPE_EXPENSE, category=self.supermercado,
            amount=Decimal("12.50"), date=dt.date(2026, 9, 10), description="Super Selectos",
        )
        c = self._scan(wallet=self.wallet)
        self.assertEqual([d["id"] for d in c["possible_duplicates"]], [ya.id])

    def test_sin_cartera_no_hay_contra_que_comparar(self):
        Transaction.objects.create(
            wallet=self.wallet, type=Transaction.TYPE_EXPENSE, category=self.supermercado,
            amount=Decimal("12.50"), date=dt.date(2026, 9, 10), description="Super Selectos",
        )
        self.assertEqual(self._scan()["possible_duplicates"], [])

    def test_sin_monto_leido_no_se_buscan_duplicados(self):
        c = self._scan({**_CRUDO, "total": ""}, wallet=self.wallet)
        self.assertEqual(c["possible_duplicates"], [])
