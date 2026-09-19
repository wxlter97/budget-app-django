"""`POST /api/v1/ai/receipt/` — el endpoint que lee un recibo.

Lo que se prueba es el borde: qué archivos entran, que nada se guarde, que un
Gemini caído no sea un 500 nuestro, y que no se pueda espiar otro workspace
pasando el UUID de una cartera ajena.
"""
import datetime as dt
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
from apps.ai.client import AIUnavailable, GeminiResponse
from apps.billing.models import Plan
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

URL = "/api/v1/ai/receipt/"
HEADER = "HTTP_X_WORKSPACE_ID"
# Se parchea el cliente de Gemini y NO `services.run`, para que el chequeo de
# cuota y el registro de consumo corran de verdad: son parte de lo que este
# endpoint tiene que hacer bien.
_GENERATE = "apps.ai.services.generate"

_CRUDO = {
    "total": "12.50",
    "currency": "USD",
    "date": "2026-09-10",
    "merchant": "Super Selectos",
    "category_hint": "Supermercado",
    "confidence": {"total": "high", "date": "medium", "merchant": "high"},
}


def _respuesta():
    return GeminiResponse(
        text=json.dumps(_CRUDO), model="gemini-2.5-flash",
        input_tokens=1500, output_tokens=300, latency_ms=900,
    )


def _archivo(name="recibo.jpg", content_type="image/jpeg", size=1024):
    return SimpleUploadedFile(name, b"x" * size, content_type=content_type)


@override_settings(GEMINI_API_KEY="k-de-prueba")
class ReceiptScanTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        grupo = Category.objects.create(
            workspace=cls.ws, name="Alimentación", type=Category.TYPE_EXPENSE
        )
        cls.supermercado = Category.objects.create(
            workspace=cls.ws, name="Supermercado", type=Category.TYPE_EXPENSE, parent=grupo,
        )
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Efectivo", currency="USD")

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _h(self):
        return {HEADER: str(self.ws.id)}

    def _post(self, **data):
        with patch(_GENERATE, return_value=_respuesta()):
            return self.client.post(URL, {"file": _archivo(), **data}, format="multipart", **self._h())

    # -- camino feliz -------------------------------------------------------
    def test_devuelve_la_candidata_con_la_confianza_por_campo(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(resp.data["amount"]), Decimal("12.50"))
        self.assertEqual(resp.data["merchant"], "Super Selectos")
        self.assertEqual(resp.data["confidence"]["date"], "medium")
        self.assertEqual(resp.data["category_source"], "ai")

    def test_no_crea_la_transaccion_ni_guarda_el_archivo(self):
        """El usuario siempre confirma: el alta y la subida del recibo pasan
        por los endpoints de siempre, después."""
        self._post()
        self.assertEqual(Transaction.objects.count(), 0)

    def test_registra_el_consumo_de_ia(self):
        self._post()
        self.assertEqual(m.AIUsage.objects.filter(operation=m.OP_RECEIPT).count(), 1)

    def test_con_cartera_devuelve_los_posibles_duplicados(self):
        ya = Transaction.objects.create(
            wallet=self.wallet, type=Transaction.TYPE_EXPENSE, category=self.supermercado,
            amount=Decimal("12.50"), date=dt.date(2026, 9, 10), description="Super Selectos",
        )
        resp = self._post(wallet=str(self.wallet.id))
        self.assertEqual([d["id"] for d in resp.data["possible_duplicates"]], [str(ya.id)])

    # -- validación de entrada ---------------------------------------------
    def test_sin_archivo_es_400(self):
        resp = self.client.post(URL, {}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_formato_no_soportado_se_rechaza_antes_de_gastar_una_llamada(self):
        with patch(_GENERATE) as run:
            resp = self.client.post(
                URL, {"file": _archivo("hoja.xlsx", "application/vnd.ms-excel")},
                format="multipart", **self._h(),
            )
        run.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_archivo_de_mas_de_8_mb_se_rechaza(self):
        with patch(_GENERATE) as run:
            resp = self.client.post(
                URL, {"file": _archivo(size=9 * 1024 * 1024)}, format="multipart", **self._h()
            )
        run.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_acepta_pdf(self):
        with patch(_GENERATE, return_value=_respuesta()):
            resp = self.client.post(
                URL, {"file": _archivo("dte.pdf", "application/pdf")},
                format="multipart", **self._h(),
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # -- permisos y aislamiento --------------------------------------------
    def test_pide_autenticacion(self):
        self.client.force_authenticate(None)
        resp = self.client.post(URL, {"file": _archivo()}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_exige_el_header_de_workspace(self):
        resp = self.client.post(URL, {"file": _archivo()}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_una_cartera_de_otro_workspace_no_deja_ver_sus_transacciones(self):
        """Sin esta validación, mandar el UUID de una cartera ajena devolvería
        montos y fechas de otro presupuesto en `possible_duplicates`."""
        ajeno = Workspace.objects.create(name="Ajeno")
        cartera_ajena = Wallet.objects.create(workspace=ajeno, name="Otra", currency="USD")
        resp = self._post(wallet=str(cartera_ajena.id))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_un_wallet_que_no_es_uuid_es_400_y_no_un_500(self):
        resp = self._post(wallet="no-soy-un-uuid")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # -- fallos de terceros y cuota ----------------------------------------
    def test_gemini_caido_es_503_y_no_un_500_nuestro(self):
        """El cliente tiene que poder distinguir "probá de nuevo o cargalo a
        mano" de "algo se rompió"."""
        with patch(_GENERATE, side_effect=AIUnavailable("timeout")):
            resp = self.client.post(URL, {"file": _archivo()}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_al_agotar_la_cuota_del_plan_responde_429(self):
        for _ in range(3):
            self.assertEqual(self._post().status_code, status.HTTP_200_OK)
        with patch(_GENERATE) as run:
            resp = self.client.post(URL, {"file": _archivo()}, format="multipart", **self._h())
        run.assert_not_called()
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(resp.data["detail"].code, "ai_quota_exceeded")

    @override_settings(GEMINI_API_KEY="")
    def test_sin_key_configurada_tambien_responde_503(self):
        resp = self.client.post(URL, {"file": _archivo()}, format="multipart", **self._h())
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
