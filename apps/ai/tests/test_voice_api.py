"""`POST /api/v1/ai/voice/` — el dictado.

Mismo borde que recibos y texto libre: qué archivos entran, que nada se
guarde, que un Gemini caído no sea un 500 nuestro, y que comparta la cuota de
`parse` (es la misma operación, ver `apps.ai.quotas.PLAN_FEATURE_KEYS`).
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
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

URL = "/api/v1/ai/voice/"
HEADER = "HTTP_X_WORKSPACE_ID"
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
        text=json.dumps(_CRUDO), model="gemini-3.8-flash",
        input_tokens=800, output_tokens=120, latency_ms=700,
    )


def _audio(name="dictado.m4a", content_type="audio/aac", size=1024):
    return SimpleUploadedFile(name, b"x" * size, content_type=content_type)


@override_settings(GEMINI_API_KEY="k-de-prueba")
class VoiceParseTests(APITestCase):
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

    def _post(self, file=None, **data):
        with patch(_GENERATE, return_value=_respuesta()):
            return self.client.post(
                URL, {"file": file or _audio(), **data}, format="multipart", **self._h()
            )

    def test_devuelve_la_candidata(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(resp.data["amount"]), Decimal("12.50"))
        self.assertEqual(resp.data["wallet"], str(self.tarjeta.id))

    def test_no_crea_la_transaccion(self):
        self._post()
        self.assertEqual(Transaction.objects.count(), 0)

    def test_registra_el_consumo_como_parseo_no_como_operacion_aparte(self):
        """El dictado comparte cuota con el texto libre: no es una operación
        nueva, es la misma entrada por otro canal (backlog, punto 3.2)."""
        self._post()
        self.assertEqual(m.AIUsage.objects.filter(operation=m.OP_PARSE).count(), 1)

    def test_marca_has_audio_en_el_registro(self):
        """El audio se costea con el precio de audio, no el de texto (ver
        `pricing.AUDIO_INPUT_PRICE_PER_MTOK`)."""
        self._post()
        row = m.AIUsage.objects.get()
        # 800 in × $0.75/Mtok (precio de audio) + 120 out × $3.75/Mtok
        self.assertEqual(row.cost_micros, 1050)

    def test_sin_archivo_es_400_y_no_gasta_una_llamada(self):
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {}, format="multipart", **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_archivo_demasiado_grande_se_rechaza_antes_de_gastar_una_llamada(self):
        grande = _audio(size=parsing.AUDIO_MAX_SIZE + 1)
        with patch(_GENERATE) as generate:
            resp = self._post(file=grande)
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_formato_no_soportado_se_rechaza(self):
        with patch(_GENERATE) as generate:
            resp = self._post(file=_audio(content_type="audio/webm"))
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pide_autenticacion(self):
        self.client.force_authenticate(None)
        resp = self.client.post(URL, {"file": _audio()}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_exige_el_header_de_workspace(self):
        resp = self.client.post(URL, {"file": _audio()}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gemini_caido_es_503_y_no_un_500_nuestro(self):
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            resp = self.client.post(URL, {"file": _audio()}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_comparte_cuota_con_el_texto_libre(self):
        """Dos dictados agotan el mismo tope de 2 que el texto libre -- son
        la misma cuota `ai_parses_per_month`."""
        self._post()
        self._post()
        with patch(_GENERATE) as generate:
            resp = self.client.post(URL, {"file": _audio()}, format="multipart", **self._h())
        generate.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
