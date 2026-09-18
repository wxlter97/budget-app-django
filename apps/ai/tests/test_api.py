"""`GET /api/v1/ai/status/` — lo que el front consulta para decidir si
muestra las entradas de IA y para avisar "te quedan N de N"."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.ai import models as m
from apps.billing.models import Plan

User = get_user_model()

STATUS_URL = "/api/v1/ai/status/"


class AIStatusTests(APITestCase):
    def setUp(self):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_pide_autenticacion(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(STATUS_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_exige_el_header_de_workspace(self):
        """La cuota es por usuario: la misma se gasta desde cualquiera de sus
        workspaces, así que pedir el header sería ruido."""
        resp = self.client.get(STATUS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    @override_settings(GEMINI_API_KEY="")
    def test_sin_key_responde_que_la_ia_esta_apagada(self):
        self.assertFalse(self.client.get(STATUS_URL).data["enabled"])

    @override_settings(GEMINI_API_KEY="k-de-prueba")
    def test_con_key_devuelve_lo_que_queda_de_cada_cuota(self):
        m.AIUsage.objects.create(
            user=self.user, operation=m.OP_RECEIPT, model="gemini-2.5-flash",
        )
        data = self.client.get(STATUS_URL).data
        self.assertTrue(data["enabled"])
        self.assertEqual(data["quotas"][m.OP_RECEIPT]["used"], 1)
        self.assertEqual(data["quotas"][m.OP_RECEIPT]["remaining"], 2)
        self.assertIsNotNone(data["resets_at"])

    def test_el_consumo_de_otro_usuario_no_aparece_en_el_mio(self):
        otro = User.objects.create_user("beto", "beto@example.com", "pw")
        m.AIUsage.objects.create(user=otro, operation=m.OP_RECEIPT, model="gemini-2.5-flash")
        data = self.client.get(STATUS_URL).data
        self.assertEqual(data["quotas"][m.OP_RECEIPT]["used"], 0)
