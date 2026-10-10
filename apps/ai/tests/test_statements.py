"""
Lectura de estados de cuenta (`apps/ai/statements.py`).

Nunca se llama a Gemini: se prueba la **normalización**, que es donde está el
riesgo (un monto inventado, una fecha de pago "corregida" a hoy, líneas
repetidas), y que la cuota se comparta con los recibos.
"""
import datetime as dt
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.ai import models as m
from apps.ai import quotas, statements
from apps.ai.client import GeminiResponse
from apps.billing.models import Plan
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

_CRUDO = {
    "statement_kind": "credit_card",
    "bank": "Banco Cuscatlán",
    "product": "Visa Platinum",
    "last4": "**** 9655",
    "currency": "usd",
    "period_start": "2026-08-16",
    "period_end": "2026-09-15",
    "payment_due_date": "2026-10-05",
    "closing_balance": "$1,250.40",
    "minimum_payment": "62.50",
    "credit_limit": "5,000.00",
    "annual_interest_rate": "28.5%",
    "transactions": [
        {"date": "2026-08-20", "description": "SUPER SELECTOS", "amount": "45.10", "direction": "expense"},
        {"date": "2026-09-01", "description": "PAGO RECIBIDO", "amount": "200.00", "direction": "income"},
    ],
    "confidence": {
        "bank": "high", "last4": "high", "closing_balance": "high",
        "period_end": "high", "transactions": "medium",
    },
}


def _respuesta(payload):
    return GeminiResponse(
        text=json.dumps(payload), model="gemini-3.8-flash",
        input_tokens=4000, output_tokens=900, latency_ms=2000,
    )


@override_settings(GEMINI_API_KEY="k-de-prueba")
class StatementTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Visa", currency="USD")

    def _candidate(self, raw=None, wallet=None):
        return statements.build_candidate(
            raw if raw is not None else _CRUDO, workspace=self.ws, wallet=wallet
        )


class NormalizacionTests(StatementTestBase):
    def test_tarjeta_arma_los_datos_de_la_cartera(self):
        w = self._candidate()["wallet"]
        self.assertEqual(w["kind"], "credit")
        self.assertEqual(w["purpose"], "debt")
        self.assertEqual(w["card_last4"], "9655")
        self.assertEqual(w["name"], "Banco Cuscatlán Visa Platinum ···· 9655")
        self.assertEqual(w["currency"], "USD")
        self.assertEqual(w["credit_limit"], Decimal("5000.00"))
        self.assertEqual(w["closing_balance"], Decimal("1250.40"))
        self.assertEqual(w["interest_rate"], Decimal("28.50"))

    def test_dia_de_corte_y_de_pago_salen_de_las_fechas(self):
        w = self._candidate()["wallet"]
        self.assertEqual(w["billing_cycle_day"], 15)
        self.assertEqual(w["payment_due_day"], 5)

    def test_la_fecha_limite_de_pago_futura_no_se_corrige_a_hoy(self):
        c = self._candidate({**_CRUDO, "payment_due_date": "2099-01-10"})
        self.assertEqual(c["payment_due_date"], dt.date(2099, 1, 10))

    def test_una_fecha_ilegible_queda_vacia_y_baja_la_confianza(self):
        c = self._candidate({**_CRUDO, "period_end": "ayer", "payment_due_date": ""})
        self.assertIsNone(c["period_end"])
        self.assertIsNone(c["wallet"]["billing_cycle_day"])
        self.assertEqual(c["confidence"]["period_end"], "low")

    def test_un_saldo_que_no_parsea_queda_vacio_aunque_el_modelo_diga_high(self):
        c = self._candidate({**_CRUDO, "closing_balance": "no se lee"})
        self.assertIsNone(c["wallet"]["closing_balance"])
        self.assertEqual(c["confidence"]["closing_balance"], "low")

    def test_saldo_cero_es_valido(self):
        c = self._candidate({**_CRUDO, "closing_balance": "0.00"})
        self.assertEqual(c["wallet"]["closing_balance"], Decimal("0.00"))

    def test_una_tasa_absurda_se_descarta(self):
        self.assertIsNone(self._candidate({**_CRUDO, "annual_interest_rate": "1250"})["wallet"]["interest_rate"])

    def test_cuenta_bancaria(self):
        w = self._candidate({**_CRUDO, "statement_kind": "bank_account"})["wallet"]
        self.assertEqual((w["kind"], w["purpose"]), ("bank", "spending"))
        self.assertIsNone(w["billing_cycle_day"])

    def test_un_tipo_desconocido_cae_a_other(self):
        self.assertEqual(self._candidate({**_CRUDO, "statement_kind": "???"})["statement_kind"], "other")

    def test_basura_no_rompe(self):
        c = self._candidate({})
        self.assertEqual(c["transactions"], [])
        self.assertEqual(c["confidence"]["transactions"], "low")
        self.assertEqual(self._candidate(raw="no es un dict")["wallet"]["name"], "Cartera nueva")


class MovimientosTests(StatementTestBase):
    def test_pago_es_ingreso_y_compra_es_gasto(self):
        tipos = {t["description"]: t["type"] for t in self._candidate()["transactions"]}
        self.assertEqual(tipos, {"SUPER SELECTOS": "expense", "PAGO RECIBIDO": "income"})

    def test_descarta_lineas_inutilizables_y_repetidas(self):
        raw = {**_CRUDO, "transactions": [
            {"date": "2026-08-20", "description": "A", "amount": "10", "direction": "expense"},
            {"date": "2026-08-20", "description": "A", "amount": "10", "direction": "expense"},  # repetida
            {"date": "2026-08-20", "description": "B", "amount": "0", "direction": "expense"},   # monto 0
            {"date": "xx", "description": "C", "amount": "5", "direction": "expense"},           # sin fecha
            {"date": "2026-08-21", "description": "", "amount": "5", "direction": "expense"},    # sin texto
            "basura",
        ]}
        self.assertEqual([t["description"] for t in self._candidate(raw)["transactions"]], ["A"])

    def test_categoria_sugerida_por_historial(self):
        grupo = Category.objects.create(workspace=self.ws, name="Alimentación", type=Category.TYPE_EXPENSE)
        cat = Category.objects.create(
            workspace=self.ws, name="Súper", type=Category.TYPE_EXPENSE, parent=grupo
        )
        Transaction.objects.create(
            wallet=self.wallet, type=Transaction.TYPE_EXPENSE, category=cat,
            amount=Decimal("20"), date=dt.date(2026, 7, 1), description="SUPER SELECTOS",
        )
        por_desc = {t["description"]: t for t in self._candidate()["transactions"]}
        self.assertEqual(por_desc["SUPER SELECTOS"]["category"], cat.id)
        self.assertIsNone(por_desc["PAGO RECIBIDO"]["category"])

    def test_marca_duplicados_contra_la_cartera(self):
        ya = Transaction.objects.create(
            wallet=self.wallet, type=Transaction.TYPE_EXPENSE,
            amount=Decimal("45.10"), date=dt.date(2026, 8, 20), description="Súper",
        )
        c = self._candidate(wallet=self.wallet)
        dup = [t for t in c["transactions"] if t["description"] == "SUPER SELECTOS"][0]
        self.assertEqual([d["id"] for d in dup["possible_duplicates"]], [ya.id])

    def test_sin_cartera_no_hay_duplicados(self):
        self.assertTrue(all(t["possible_duplicates"] == [] for t in self._candidate()["transactions"]))


class CuotaCompartidaTests(StatementTestBase):
    def test_los_estados_de_cuenta_descuentan_de_la_bolsa_de_recibos(self):
        for op in (m.OP_RECEIPT, m.OP_STATEMENT, m.OP_STATEMENT):
            m.AIUsage.objects.create(
                user=self.user, workspace=self.ws, operation=op, model="x", counts_against_quota=True,
            )
        # Bolsa de 3: dos estados + un recibo = agotada, para cualquiera de los dos.
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 3)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_STATEMENT), 3)
        with self.assertRaises(quotas.QuotaExceeded):
            quotas.check(self.user, m.OP_STATEMENT)


@override_settings(GEMINI_API_KEY="k-de-prueba")
class StatementApiTests(APITestCase):
    URL = "/api/v1/ai/statement/"

    @classmethod
    def setUpTestData(cls):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        cls.user = User.objects.create_user("ana", "ana@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _post(self, file=None):
        file = file or SimpleUploadedFile("estado.pdf", b"%PDF-1.4 falso", content_type="application/pdf")
        with patch("apps.ai.services.generate", return_value=_respuesta(_CRUDO)):
            return self.client.post(
                self.URL, {"file": file}, format="multipart", HTTP_X_WORKSPACE_ID=str(self.ws.id)
            )

    def test_devuelve_la_candidata_y_no_crea_nada(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["wallet"]["card_last4"], "9655")
        self.assertEqual(len(resp.data["transactions"]), 2)
        self.assertEqual(Wallet.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(m.AIUsage.objects.filter(operation=m.OP_STATEMENT).count(), 1)

    def test_rechaza_formato_no_soportado(self):
        resp = self._post(SimpleUploadedFile("x.docx", b"x", content_type="application/msword"))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_exige_autenticacion(self):
        self.client.force_authenticate(None)
        self.assertEqual(self._post().status_code, status.HTTP_401_UNAUTHORIZED)
