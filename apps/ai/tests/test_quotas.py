"""
Cuota mensual de IA por plan (`apps/ai/quotas.py`).

Lo que se prueba acá es lo que protege la factura: que el tope salga del plan
y no del código, que un plan incompleto no se convierta en barra libre, y que
el contador sea del mes calendario.
"""
import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.ai import models as m
from apps.ai import quotas
from apps.billing.models import Plan, Subscription

User = get_user_model()


def usage(user, operation, *, when=None, counts=True, status=m.STATUS_OK):
    row = m.AIUsage.objects.create(
        user=user, operation=operation, model="gemini-2.5-flash",
        status=status, counts_against_quota=counts,
    )
    if when is not None:
        # `created_at` es auto_now_add: para poner una fecha vieja hay que
        # pisarla después de crear.
        m.AIUsage.objects.filter(pk=row.pk).update(created_at=when)
    return row


class LimitesPorPlanTests(TestCase):
    def setUp(self):
        self.free = Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def test_el_tope_sale_del_plan_y_no_del_codigo(self):
        self.assertEqual(quotas.limit_for(self.user, m.OP_RECEIPT), 3)
        self.free.features["ai_receipts_per_month"] = 25
        self.free.save()
        self.assertEqual(quotas.limit_for(self.user, m.OP_RECEIPT), 25)

    def test_usa_el_plan_de_la_suscripcion_vigente(self):
        pro = Plan.objects.create(
            code="pro", name="Pro",
            features={"ai_receipts_per_month": 100, "ai_parses_per_month": 200, "ai_chats_per_month": 100},
        )
        Subscription.objects.create(
            user=self.user, plan=pro, status=Subscription.STATUS_ACTIVE,
        )
        self.assertEqual(quotas.limit_for(self.user, m.OP_RECEIPT), 100)

    def test_un_plan_sin_la_clave_cae_en_los_numeros_del_gratis_y_no_en_ilimitado(self):
        """Fail-closed a propósito, al revés que el resto de los feature flags:
        acá el costo de equivocarse es una factura, no una pantalla de más."""
        self.free.features = {}
        self.free.save()
        self.assertEqual(quotas.limit_for(self.user, m.OP_RECEIPT), 3)
        self.assertEqual(quotas.limit_for(self.user, m.OP_CHAT), 0)

    def test_un_valor_basura_en_el_admin_tampoco_abre_la_llave(self):
        self.free.features["ai_receipts_per_month"] = "muchos"
        self.free.save()
        self.assertEqual(quotas.limit_for(self.user, m.OP_RECEIPT), 3)

    def test_null_explicito_es_sin_tope(self):
        self.free.features["ai_receipts_per_month"] = None
        self.free.save()
        self.assertIsNone(quotas.limit_for(self.user, m.OP_RECEIPT))

    def test_el_resumen_mensual_no_gasta_cuota(self):
        """Lo dispara el servidor, no el usuario."""
        self.assertIsNone(quotas.limit_for(self.user, m.OP_SUMMARY))


class ContadorTests(TestCase):
    def setUp(self):
        Plan.objects.create(
            code="free", name="Gratis", is_default=True,
            features={"ai_receipts_per_month": 3, "ai_parses_per_month": 10, "ai_chats_per_month": 0},
        )
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")

    def test_cuenta_solo_las_de_este_mes(self):
        usage(self.user, m.OP_RECEIPT)
        usage(self.user, m.OP_RECEIPT, when=quotas.month_start() - dt.timedelta(days=1))
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 1)

    def test_cuenta_solo_la_operacion_pedida(self):
        usage(self.user, m.OP_RECEIPT)
        usage(self.user, m.OP_PARSE)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 1)

    def test_una_llamada_que_fallo_no_le_come_la_cuota_al_usuario(self):
        usage(self.user, m.OP_RECEIPT, counts=False, status=m.STATUS_ERROR)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 0)

    def test_la_cuota_de_un_usuario_no_es_la_de_otro(self):
        otro = User.objects.create_user("beto", "beto@example.com", "pw")
        usage(otro, m.OP_RECEIPT)
        usage(otro, m.OP_RECEIPT)
        self.assertEqual(quotas.used_this_month(self.user, m.OP_RECEIPT), 0)

    def test_check_deja_pasar_hasta_el_tope_y_corta_despues(self):
        for _ in range(3):
            quotas.check(self.user, m.OP_RECEIPT)
            usage(self.user, m.OP_RECEIPT)
        with self.assertRaises(quotas.QuotaExceeded) as ctx:
            quotas.check(self.user, m.OP_RECEIPT)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.get_codes(), "ai_quota_exceeded")

    def test_con_tope_cero_el_mensaje_invita_a_cambiar_de_plan(self):
        with self.assertRaises(quotas.QuotaExceeded) as ctx:
            quotas.check(self.user, m.OP_CHAT)
        self.assertIn("no incluye", str(ctx.exception))

    def test_el_status_arma_lo_que_el_front_necesita(self):
        usage(self.user, m.OP_RECEIPT)
        data = quotas.status_for(self.user)
        self.assertEqual(data["quotas"][m.OP_RECEIPT], {"limit": 3, "used": 1, "remaining": 2})
        self.assertEqual(data["quotas"][m.OP_CHAT]["remaining"], 0)
        self.assertNotIn(m.OP_SUMMARY, data["quotas"])


class VentanaDelMesTests(TestCase):
    def test_el_reset_de_diciembre_cae_en_enero_del_ano_siguiente(self):
        diciembre = timezone.make_aware(dt.datetime(2026, 12, 14, 10, 0))
        self.assertEqual(quotas.next_reset(diciembre).year, 2027)
        self.assertEqual(quotas.next_reset(diciembre).month, 1)

    def test_el_mes_arranca_el_dia_uno_a_las_cero(self):
        start = quotas.month_start(timezone.make_aware(dt.datetime(2026, 3, 18, 15, 30)))
        self.assertEqual((start.day, start.hour, start.minute), (1, 0, 0))
