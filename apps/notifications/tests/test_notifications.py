import datetime as dt
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.notifications import services
from apps.notifications.models import Notification, NotificationLog, NotificationPreference, PushDevice
from apps.transactions.models import Category, CategoryBudget, RecurringExpense, Transaction
from apps.workspaces.models import Invitation, Membership, Workspace

User = get_user_model()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class PushDeviceApiTests(APITestCase):
    URL = "/api/v1/push-devices/"

    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@example.com", "pw")
        self.bob = User.objects.create_user("bob", "b@example.com", "pw")

    def test_register_creates_device(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(self.URL, {"token": "ExponentPushToken[abc]", "platform": "ios"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(PushDevice.objects.get().user, self.alice)

    def test_reregistering_same_token_reassigns_owner(self):
        PushDevice.objects.create(user=self.bob, token="ExponentPushToken[shared]")
        self.client.force_authenticate(self.alice)
        resp = self.client.post(self.URL, {"token": "ExponentPushToken[shared]"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PushDevice.objects.count(), 1)
        self.assertEqual(PushDevice.objects.get().user, self.alice)

    def test_unregister_removes_device(self):
        PushDevice.objects.create(user=self.alice, token="ExponentPushToken[x]")
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{self.URL}unregister/", {"token": "ExponentPushToken[x]"})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PushDevice.objects.exists())

    def test_unregister_requires_token(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{self.URL}unregister/", {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_unregister_someone_elses_device(self):
        PushDevice.objects.create(user=self.bob, token="ExponentPushToken[bob]")
        self.client.force_authenticate(self.alice)
        self.client.post(f"{self.URL}unregister/", {"token": "ExponentPushToken[bob]"})
        self.assertTrue(PushDevice.objects.filter(user=self.bob).exists())

    def test_web_platform_requires_p256dh_and_auth(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            self.URL, {"token": "https://push.example.com/sub/1", "platform": "web"}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_web_platform_registers_with_keys(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            self.URL,
            {
                "token": "https://push.example.com/sub/1",
                "platform": "web",
                "p256dh": "pkey",
                "auth": "akey",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        device = PushDevice.objects.get()
        self.assertEqual(device.p256dh, "pkey")
        self.assertEqual(device.auth, "akey")

    @override_settings(VAPID_PUBLIC_KEY="pub-key-for-the-browser")
    def test_vapid_public_key_is_public(self):
        # Sin autenticar a propósito: hace falta ANTES de que el navegador
        # pueda registrar nada.
        resp = self.client.get(f"{self.URL}vapid-public-key/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["vapid_public_key"], "pub-key-for-the-browser")


class NotificationPreferenceApiTests(APITestCase):
    URL = "/api/v1/notification-preferences/"

    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_get_creates_defaults(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["remind_recurring"])
        self.assertEqual(resp.data["budget_threshold_pct"], 90)
        self.assertEqual(NotificationPreference.objects.count(), 1)

    def test_patch_updates(self):
        resp = self.client.patch(self.URL, {"remind_recurring": False, "budget_threshold_pct": 75})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(resp.data["remind_recurring"])
        self.assertEqual(resp.data["budget_threshold_pct"], 75)

    def test_threshold_out_of_range_rejected(self):
        resp = self.client.patch(self.URL, {"budget_threshold_pct": 10})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Servicios (lógica de recordatorios)
# ---------------------------------------------------------------------------
class NotificationServicesTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.workspace, user=self.user, role=Membership.ROLE_OWNER)
        self.wallet = Wallet.objects.create(
            workspace=self.workspace, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING
        )
        self.category = Category.objects.create(
            workspace=self.workspace, name="Servicios", type=Category.TYPE_EXPENSE
        )
        self.device = PushDevice.objects.create(user=self.user, token="ExponentPushToken[x]")


class NotifyDueItemsTests(NotificationServicesTestCase):
    def setUp(self):
        super().setUp()
        self.tomorrow = timezone.localdate() + timedelta(days=1)
        self.recurring = RecurringExpense.objects.create(
            workspace=self.workspace, category=self.category, wallet=self.wallet,
            amount=Decimal("50.00"), frequency=RecurringExpense.FREQUENCY_MONTHLY,
            next_due_date=self.tomorrow,
        )

    @patch("apps.notifications.services.send_push")
    def test_sends_reminder_for_recurring_due_tomorrow(self, mock_send):
        services.notify_due_items()
        mock_send.assert_called_once()
        devices, kwargs = mock_send.call_args[0][0], mock_send.call_args[1]
        self.assertEqual(devices, [self.device])
        self.assertIn("mañana", kwargs["title"].lower())
        self.assertTrue(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_RECURRING_DUE
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_income_recurring_due_tomorrow_is_not_titled_as_expense(self, mock_send):
        # `self.recurring` (de setUp) ya venció mañana como gasto -- se
        # desactiva para que esta prueba sea sobre un solo ítem, el ingreso.
        self.recurring.is_active = False
        self.recurring.save()
        income_cat = Category.objects.create(
            workspace=self.workspace, name="Sueldo", type=Category.TYPE_INCOME
        )
        RecurringExpense.objects.create(
            workspace=self.workspace, category=income_cat, wallet=self.wallet,
            type=RecurringExpense.TYPE_INCOME, amount=Decimal("1200.00"),
            frequency=RecurringExpense.FREQUENCY_MONTHLY, next_due_date=self.tomorrow,
        )
        services.notify_due_items()
        mock_send.assert_called_once()
        title = mock_send.call_args[1]["title"]
        self.assertIn("Ingreso", title)
        self.assertNotIn("Gasto", title)

    @patch("apps.notifications.services.send_push")
    def test_does_not_resend_same_day(self, mock_send):
        services.notify_due_items()
        services.notify_due_items()
        self.assertEqual(mock_send.call_count, 1)

    @patch("apps.notifications.services.send_push")
    def test_respects_preference_off(self, mock_send):
        NotificationPreference.objects.create(user=self.user, remind_recurring=False)
        services.notify_due_items()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_no_reminder_when_no_device(self, mock_send):
        self.device.delete()
        services.notify_due_items()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_still_creates_in_app_notification_without_a_device(self, mock_send):
        # Sin dispositivo no hay push, pero el centro de notificaciones de
        # la app tiene que verlo igual -- no depende de tener uno registrado.
        self.device.delete()
        services.notify_due_items()
        mock_send.assert_not_called()
        self.assertTrue(
            Notification.objects.filter(
                user=self.user, kind=Notification.KIND_RECURRING_DUE, status=Notification.STATUS_UNREAD
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_no_reminder_when_due_date_is_further_out(self, mock_send):
        self.recurring.next_due_date = timezone.localdate() + timedelta(days=5)
        self.recurring.save(update_fields=["next_due_date"])
        services.notify_due_items()
        mock_send.assert_not_called()


class NotifyRespectsPlanGateTests(NotificationServicesTestCase):
    """`notifications` es una de las funciones fuera del gratis desde el
    22-sep-2026 (ver seed_billing_plans.py) -- el gate vive en `_notify`
    (apps/notifications/services.py), no en `notify_user`, para no tocar
    invitaciones ni avisos de suscripción (esos siguen en el gratis)."""

    def setUp(self):
        super().setUp()
        self.tomorrow = timezone.localdate() + timedelta(days=1)
        self.recurring = RecurringExpense.objects.create(
            workspace=self.workspace, category=self.category, wallet=self.wallet,
            amount=Decimal("50.00"), frequency=RecurringExpense.FREQUENCY_MONTHLY,
            next_due_date=self.tomorrow,
        )

    @patch("apps.notifications.services.send_push")
    def test_free_workspace_gets_no_reminder_push_nor_log(self, mock_send):
        from apps.billing.models import Plan

        Plan.objects.create(
            code="free", name="Gratis", is_default=True, features={"notifications": False}
        )
        services.notify_due_items()
        mock_send.assert_not_called()
        self.assertFalse(
            Notification.objects.filter(
                user=self.user, kind=Notification.KIND_RECURRING_DUE
            ).exists()
        )
        self.assertFalse(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_RECURRING_DUE
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_plan_with_the_feature_still_gets_reminders(self, mock_send):
        from apps.billing.models import Plan

        Plan.objects.create(
            code="free", name="Gratis", is_default=True, features={"notifications": True}
        )
        services.notify_due_items()
        mock_send.assert_called_once()


class NotifyBudgetThresholdsTests(NotificationServicesTestCase):
    def _spend(self, amount):
        Transaction.objects.create(
            wallet=self.wallet, category=self.category, amount=Decimal(amount),
            date=timezone.localdate(), type=Transaction.TYPE_EXPENSE,
        )

    def _budget(self, amount):
        today = timezone.localdate()
        CategoryBudget.objects.create(
            workspace=self.workspace, category=self.category, amount=Decimal(amount),
            period_start=today.replace(day=1),  # workspace.budget_period default: monthly
        )

    @patch("apps.notifications.services.send_push")
    def test_warns_when_threshold_crossed(self, mock_send):
        self._budget("100.00")
        self._spend("95.00")
        services.notify_budget_thresholds()
        mock_send.assert_called_once()
        self.assertTrue(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_BUDGET_THRESHOLD
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_no_warning_below_threshold(self, mock_send):
        self._budget("100.00")
        self._spend("50.00")
        services.notify_budget_thresholds()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_no_warning_without_budget(self, mock_send):
        self._spend("500.00")
        services.notify_budget_thresholds()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_respects_custom_threshold(self, mock_send):
        NotificationPreference.objects.create(user=self.user, budget_threshold_pct=60)
        self._budget("100.00")
        self._spend("65.00")
        services.notify_budget_thresholds()
        mock_send.assert_called_once()

    @patch("apps.notifications.services.send_push")
    def test_respects_preference_off(self, mock_send):
        NotificationPreference.objects.create(user=self.user, warn_budget=False)
        self._budget("100.00")
        self._spend("99.00")
        services.notify_budget_thresholds()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_does_not_rewarn_same_month(self, mock_send):
        self._budget("100.00")
        self._spend("95.00")
        services.notify_budget_thresholds()
        services.notify_budget_thresholds()
        self.assertEqual(mock_send.call_count, 1)


class NotifyLowBalanceTests(NotificationServicesTestCase):
    def setUp(self):
        super().setUp()
        self.wallet.low_balance_threshold = Decimal("50.00")
        self.wallet.opening_balance = Decimal("20.00")
        self.wallet.current_balance = Decimal("20.00")
        self.wallet.save(update_fields=["low_balance_threshold", "opening_balance", "current_balance"])

    @patch("apps.notifications.services.send_push")
    def test_warns_when_balance_below_threshold(self, mock_send):
        services.notify_low_balance()
        mock_send.assert_called_once()
        self.assertTrue(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_LOW_BALANCE
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_no_warning_above_threshold(self, mock_send):
        self.wallet.current_balance = Decimal("100.00")
        self.wallet.save(update_fields=["current_balance"])
        services.notify_low_balance()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_no_warning_without_threshold_set(self, mock_send):
        self.wallet.low_balance_threshold = None
        self.wallet.save(update_fields=["low_balance_threshold"])
        services.notify_low_balance()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_respects_preference_off(self, mock_send):
        NotificationPreference.objects.create(user=self.user, remind_low_balance=False)
        services.notify_low_balance()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_does_not_rewarn_same_month(self, mock_send):
        services.notify_low_balance()
        services.notify_low_balance()
        self.assertEqual(mock_send.call_count, 1)

    @patch("apps.notifications.services.send_push")
    def test_archived_wallet_is_skipped(self, mock_send):
        self.wallet.is_archived = True
        self.wallet.save(update_fields=["is_archived"])
        services.notify_low_balance()
        mock_send.assert_not_called()


class NotifyStatementDueTests(NotificationServicesTestCase):
    """Fechas todas fijas (nunca `timezone.localdate()` real): con
    `billing_cycle_day=1` y `payment_due_day=10`, el corte de marzo cae el
    2026-03-01 y su pago de contado vence el 2026-03-10 -- ese vencimiento es
    el que cada test ubica "hoy" antes o después."""

    DUE_DATE = dt.date(2026, 3, 10)

    def setUp(self):
        super().setUp()
        self.card = Wallet.objects.create(
            workspace=self.workspace, name="Tarjeta", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, billing_cycle_day=1, payment_due_day=10,
            credit_limit=Decimal("1000.00"),
        )
        Transaction.objects.create(
            wallet=self.card, category=self.category, amount=Decimal("300.00"),
            date=dt.date(2026, 3, 1),
        )

    @patch("apps.notifications.services.send_push")
    def test_warns_when_due_within_window(self, mock_send):
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        mock_send.assert_called_once()
        self.assertTrue(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_STATEMENT_DUE
            ).exists()
        )

    @patch("apps.notifications.services.send_push")
    def test_no_warning_far_from_due_date(self, mock_send):
        # Todavía a 9 días del vencimiento -- más lejos que el default (3).
        with patch(
            "django.utils.timezone.localdate", return_value=self.DUE_DATE - timedelta(days=9)
        ):
            services.notify_statement_due()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_no_warning_after_due_date_passed(self, mock_send):
        with patch(
            "django.utils.timezone.localdate", return_value=self.DUE_DATE + timedelta(days=1)
        ):
            services.notify_statement_due()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_no_warning_without_balance_due(self, mock_send):
        # Sin transacciones, el pago de contado es 0 -- nada que avisar.
        self.card.transactions.all().delete()
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_respects_preference_off(self, mock_send):
        NotificationPreference.objects.create(user=self.user, warn_statement_due=False)
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_does_not_rewarn_within_the_same_window(self, mock_send):
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE - timedelta(days=2)):
            services.notify_statement_due()
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        self.assertEqual(mock_send.call_count, 1)

    def _pay_card(self, amount, on):
        bank = Wallet.objects.create(workspace=self.workspace, name="Banco")
        Transaction.objects.create(
            wallet=bank, to_wallet=self.card, type=Transaction.TYPE_TRANSFER,
            amount=Decimal(amount), date=on,
        )

    @patch("apps.notifications.services.send_push")
    def test_no_warning_when_statement_already_paid(self, mock_send):
        self._pay_card("300.00", dt.date(2026, 3, 5))
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        mock_send.assert_not_called()

    @patch("apps.notifications.services.send_push")
    def test_amount_is_statement_balance_minus_payments_not_todays_balance(self, mock_send):
        # Compra DESPUÉS del corte: se cobra en el corte siguiente, no entra.
        Transaction.objects.create(
            wallet=self.card, category=self.category, amount=Decimal("50.00"),
            date=dt.date(2026, 3, 4),
        )
        self._pay_card("100.00", dt.date(2026, 3, 5))
        with patch("django.utils.timezone.localdate", return_value=self.DUE_DATE):
            services.notify_statement_due()
        mock_send.assert_called_once()
        data = mock_send.call_args.kwargs["data"]
        self.assertEqual(data["amount"], "200.00")
        self.assertEqual(data["due_date"], "2026-03-10")
        self.assertEqual(mock_send.call_args.kwargs["title"], "Fecha límite de pago")
        self.assertIn("para no generar intereses", mock_send.call_args.kwargs["body"])


class NotifyInsightsTests(NotificationServicesTestCase):
    """`behavior_insights` (los 6 detectores) ya se prueba a fondo en
    `apps.reports.tests.test_behavior_insights` -- acá sólo importa el
    "pegamento": que `notify_insights` respete la preferencia, no repita el
    mismo patrón, y arme la notificación con lo que ese detector devuelve.
    Por eso se mockea `behavior_insights` en vez de armar transacciones
    reales.

    Los patrones se calculan un solo día de la semana (ver
    `services.INSIGHTS_WEEKDAY`), así que las pruebas pasan un lunes
    explícito en vez de depender del día en que se corra la suite."""

    MONDAY = dt.date(2026, 3, 30)

    ONE_INSIGHT = [
        {"dedupe_key": "ws:weekend:2026-W10", "title": "Gastás más los fines de semana", "body": "..."},
    ]
    TWO_INSIGHTS = ONE_INSIGHT + [
        {"dedupe_key": "ws:peak_day:2026-W10", "title": "Tenés un día pico", "body": "..."},
    ]

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_creates_a_notification_per_insight_and_sends_push(self, mock_insights, mock_send):
        mock_insights.return_value = self.TWO_INSIGHTS
        services.notify_insights(today=self.MONDAY)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(
            Notification.objects.filter(user=self.user, kind=Notification.KIND_INSIGHT).count(), 2
        )
        self.assertEqual(
            NotificationLog.objects.filter(user=self.user, kind=NotificationLog.KIND_INSIGHT).count(), 2
        )

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_respects_preference_off(self, mock_insights, mock_send):
        NotificationPreference.objects.create(user=self.user, warn_insights=False)
        mock_insights.return_value = self.ONE_INSIGHT
        services.notify_insights(today=self.MONDAY)
        mock_send.assert_not_called()
        self.assertFalse(Notification.objects.filter(kind=Notification.KIND_INSIGHT).exists())
        mock_insights.assert_not_called()

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_does_not_repeat_the_same_dedupe_key(self, mock_insights, mock_send):
        mock_insights.return_value = self.ONE_INSIGHT
        services.notify_insights(today=self.MONDAY)
        services.notify_insights(today=self.MONDAY)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(Notification.objects.filter(kind=Notification.KIND_INSIGHT).count(), 1)

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_still_creates_in_app_notification_without_a_device(self, mock_insights, mock_send):
        self.device.delete()
        mock_insights.return_value = self.ONE_INSIGHT
        services.notify_insights(today=self.MONDAY)
        mock_send.assert_not_called()
        self.assertTrue(Notification.objects.filter(kind=Notification.KIND_INSIGHT).exists())

    @patch("apps.notifications.services.behavior_insights")
    def test_does_not_compute_anything_outside_the_weekly_slot(self, mock_insights):
        services.notify_insights(today=self.MONDAY + timedelta(days=1))
        mock_insights.assert_not_called()


class NotifyMonthlySummaryTests(NotificationServicesTestCase):
    """Mismo criterio que `NotifyInsightsTests`: `behavior_insights` se
    mockea (ya se prueba aparte), acá sólo importa que `notify_monthly_summary`
    dispare el día correcto, respete la preferencia, dedupee por mes y arme
    el texto con lo que devuelva `apps.ai.summary.generate` (o el respaldo si
    no está disponible)."""

    FIRST_OF_MONTH = dt.date(2026, 4, 1)

    INSIGHTS = [
        {"dedupe_key": "ws:weekend:2026-W13", "title": "Gastás más los fines de semana", "body": "..."},
        {"dedupe_key": "ws:peak_day:2026-W13", "title": "Tenés un día pico", "body": "..."},
    ]

    @patch("apps.ai.summary.generate")
    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_creates_notification_with_ai_generated_text(self, mock_insights, mock_send, mock_generate):
        mock_insights.return_value = self.INSIGHTS
        mock_generate.return_value = {"title": "Tu marzo", "body": "Gastaste más los fines de semana."}
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)

        mock_insights.assert_called_once_with(self.workspace, self.user, today=dt.date(2026, 3, 31))
        mock_send.assert_called_once()
        notif = Notification.objects.get(user=self.user, kind=Notification.KIND_MONTHLY_SUMMARY)
        self.assertEqual(notif.title, "Tu marzo")
        self.assertEqual(notif.body, "Gastaste más los fines de semana.")
        self.assertTrue(
            NotificationLog.objects.filter(
                user=self.user, kind=NotificationLog.KIND_MONTHLY_SUMMARY, dedupe_key=f"{self.workspace.id}:2026-03"
            ).exists()
        )

    @patch("apps.ai.summary.generate")
    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_falls_back_to_handwritten_text_when_ai_unavailable(self, mock_insights, mock_send, mock_generate):
        from apps.ai.client import AIUnavailable

        mock_insights.return_value = self.INSIGHTS
        mock_generate.side_effect = AIUnavailable("no_api_key")
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)

        mock_send.assert_called_once()
        notif = Notification.objects.get(user=self.user, kind=Notification.KIND_MONTHLY_SUMMARY)
        self.assertEqual(notif.title, "Tu resumen del mes")
        self.assertIn("Gastás más los fines de semana", notif.body)
        self.assertIn("Tenés un día pico", notif.body)

    @patch("apps.notifications.services.behavior_insights")
    def test_does_not_run_outside_the_first_of_the_month(self, mock_insights):
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH + timedelta(days=1))
        mock_insights.assert_not_called()

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_respects_preference_off(self, mock_insights, mock_send):
        NotificationPreference.objects.create(user=self.user, warn_monthly_summary=False)
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)
        mock_send.assert_not_called()
        mock_insights.assert_not_called()
        self.assertFalse(Notification.objects.filter(kind=Notification.KIND_MONTHLY_SUMMARY).exists())

    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_no_notification_without_insights(self, mock_insights, mock_send):
        mock_insights.return_value = []
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)
        mock_send.assert_not_called()
        self.assertFalse(Notification.objects.filter(kind=Notification.KIND_MONTHLY_SUMMARY).exists())

    @patch("apps.ai.summary.generate")
    @patch("apps.notifications.services.send_push")
    @patch("apps.notifications.services.behavior_insights")
    def test_does_not_repeat_the_same_month(self, mock_insights, mock_send, mock_generate):
        mock_insights.return_value = self.INSIGHTS
        mock_generate.return_value = {"title": "Tu marzo", "body": "..."}
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)
        services.notify_monthly_summary(today=self.FIRST_OF_MONTH)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            Notification.objects.filter(kind=Notification.KIND_MONTHLY_SUMMARY).count(), 1
        )


class SendPushTests(TestCase):
    @patch("apps.notifications.services.urllib_request.urlopen")
    def test_posts_to_expo_with_batched_messages(self, mock_urlopen):
        devices = [PushDevice(token=f"tok{i}") for i in range(3)]
        services.send_push(devices, title="Hola", body="Mundo")
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_no_devices_does_not_call_network(self):
        with patch("apps.notifications.services.urllib_request.urlopen") as mock_urlopen:
            services.send_push([], title="x", body="y")
            mock_urlopen.assert_not_called()

    @patch("apps.notifications.services.urllib_request.urlopen", side_effect=OSError("boom"))
    def test_network_error_does_not_raise(self, mock_urlopen):
        devices = [PushDevice(token="tok")]
        # No debe lanzar aunque Expo esté caído -- se loggea y se sigue.
        services.send_push(devices, title="x", body="y")

    @patch("apps.notifications.services.urllib_request.urlopen")
    def test_web_devices_never_go_to_expo(self, mock_urlopen):
        web_device = PushDevice(
            token="https://fcm.googleapis.com/x", platform=PushDevice.PLATFORM_WEB,
            p256dh="pkey", auth="akey",
        )
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY=""):
            services.send_push([web_device], title="x", body="y")
        mock_urlopen.assert_not_called()


@override_settings(VAPID_PUBLIC_KEY="pub-key", VAPID_PRIVATE_KEY="priv-key")
class SendWebPushTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("erin", "e@example.com", "pw")
        self.device = PushDevice.objects.create(
            user=self.user, token="https://push.example.com/sub/1",
            platform=PushDevice.PLATFORM_WEB, p256dh="pkey", auth="akey",
        )

    @patch("pywebpush.webpush")
    def test_calls_webpush_with_subscription_and_vapid_claims(self, mock_webpush):
        services.send_push([self.device], title="Hola", body="Mundo")
        mock_webpush.assert_called_once()
        kwargs = mock_webpush.call_args.kwargs
        self.assertEqual(
            kwargs["subscription_info"],
            {"endpoint": self.device.token, "keys": {"p256dh": "pkey", "auth": "akey"}},
        )
        self.assertEqual(kwargs["vapid_private_key"], "priv-key")
        self.assertEqual(kwargs["vapid_claims"], {"sub": settings.VAPID_SUBJECT})
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["title"], "Hola")
        self.assertEqual(payload["body"], "Mundo")

    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    @patch("pywebpush.webpush")
    def test_skips_without_vapid_configured(self, mock_webpush):
        services.send_push([self.device], title="x", body="y")
        mock_webpush.assert_not_called()

    @patch("pywebpush.webpush")
    def test_expired_subscription_is_deleted(self, mock_webpush):
        from pywebpush import WebPushException

        response = type("Resp", (), {"status_code": 410})()
        mock_webpush.side_effect = WebPushException("gone", response=response)
        services.send_push([self.device], title="x", body="y")
        self.assertFalse(PushDevice.objects.filter(id=self.device.id).exists())

    @patch("pywebpush.webpush")
    def test_other_error_does_not_delete_the_device(self, mock_webpush):
        from pywebpush import WebPushException

        response = type("Resp", (), {"status_code": 500})()
        mock_webpush.side_effect = WebPushException("server error", response=response)
        services.send_push([self.device], title="x", body="y")
        self.assertTrue(PushDevice.objects.filter(id=self.device.id).exists())

    @patch("pywebpush.webpush", side_effect=OSError("network down"))
    def test_network_error_does_not_raise(self, mock_webpush):
        services.send_push([self.device], title="x", body="y")


@override_settings(VAPID_PUBLIC_KEY="pub-key", VAPID_PRIVATE_KEY="priv-key")
class SendTestPushTests(TestCase):
    """El aviso de prueba dice qué pasó con cada dispositivo, para saber si el
    problema está en el servidor o en el navegador."""

    def setUp(self):
        self.user = User.objects.create_user("erin", "e@example.com", "pw")
        self.device = PushDevice.objects.create(
            user=self.user, token="https://push.example.com/sub/1",
            platform=PushDevice.PLATFORM_WEB, p256dh="pkey", auth="akey",
        )

    @patch("pywebpush.webpush")
    def test_reports_delivered(self, mock_webpush):
        mock_webpush.return_value = type("Resp", (), {"status_code": 201})()
        [result] = services.send_test_push(self.user)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 201)
        self.assertEqual(json.loads(mock_webpush.call_args.kwargs["data"])["title"], "Aviso de prueba")

    @patch("pywebpush.webpush")
    def test_reports_a_rejection_with_its_status(self, mock_webpush):
        from pywebpush import WebPushException

        mock_webpush.side_effect = WebPushException(
            "forbidden", response=type("Resp", (), {"status_code": 403})()
        )
        [result] = services.send_test_push(self.user)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertTrue(PushDevice.objects.filter(id=self.device.id).exists())

    @patch("pywebpush.webpush")
    def test_an_expired_subscription_is_reported_and_removed(self, mock_webpush):
        from pywebpush import WebPushException

        mock_webpush.side_effect = WebPushException(
            "gone", response=type("Resp", (), {"status_code": 410})()
        )
        [result] = services.send_test_push(self.user)
        self.assertFalse(result["ok"])
        self.assertIn("volver a activarla", result["detail"])
        self.assertFalse(PushDevice.objects.filter(id=self.device.id).exists())

    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_reports_missing_vapid(self):
        [result] = services.send_test_push(self.user)
        self.assertFalse(result["ok"])
        self.assertIn("VAPID", result["detail"])

    def test_without_devices_returns_nothing(self):
        PushDevice.objects.all().delete()
        self.assertEqual(services.send_test_push(self.user), [])

    @patch("pywebpush.webpush")
    def test_the_endpoint_only_touches_the_callers_devices(self, mock_webpush):
        from rest_framework.test import APIClient

        mock_webpush.return_value = type("Resp", (), {"status_code": 201})()
        other = User.objects.create_user("zed", "z@example.com", "pw")
        PushDevice.objects.create(
            user=other, token="https://push.example.com/sub/2",
            platform=PushDevice.PLATFORM_WEB, p256dh="p", auth="a",
        )
        client = APIClient()
        self.assertEqual(client.post("/api/v1/push-devices/test/").status_code, 401)
        client.force_authenticate(self.user)
        res = client.post("/api/v1/push-devices/test/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["devices"], 1)
        self.assertEqual(mock_webpush.call_count, 1)


# ---------------------------------------------------------------------------
# Centro de notificaciones -- API
# ---------------------------------------------------------------------------
class NotificationApiTests(APITestCase):
    LIST = "/api/v1/notifications/"

    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@example.com", "pw")
        self.bob = User.objects.create_user("bob", "b@example.com", "pw")
        self.client.force_authenticate(self.alice)

    def _make(self, user, **kwargs):
        defaults = dict(
            kind=Notification.KIND_LOW_BALANCE, title="Saldo bajo", body="...",
            status=Notification.STATUS_UNREAD,
        )
        defaults.update(kwargs)
        return Notification.objects.create(user=user, **defaults)

    def test_only_lists_own_notifications(self):
        mine = self._make(self.alice)
        self._make(self.bob)
        resp = self.client.get(self.LIST)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in resp.data["results"]}
        self.assertEqual(ids, {str(mine.id)})

    def test_list_includes_resolved_ones_too_not_just_unread(self):
        # Es un historial, no solo una bandeja de pendientes.
        self._make(self.alice, status=Notification.STATUS_RESOLVED)
        resp = self.client.get(self.LIST)
        self.assertEqual(len(resp.data["results"]), 1)

    def test_unread_count_only_counts_unread(self):
        self._make(self.alice, status=Notification.STATUS_UNREAD)
        self._make(self.alice, status=Notification.STATUS_READ)
        self._make(self.alice, status=Notification.STATUS_RESOLVED)
        resp = self.client.get(f"{self.LIST}unread-count/")
        self.assertEqual(resp.data["count"], 1)

    def test_read_marks_it_read(self):
        n = self._make(self.alice)
        resp = self.client.post(f"{self.LIST}{n.id}/read/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.STATUS_READ)

    def test_read_does_not_downgrade_a_resolved_one_back_to_read(self):
        n = self._make(self.alice, status=Notification.STATUS_RESOLVED)
        self.client.post(f"{self.LIST}{n.id}/read/")
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.STATUS_RESOLVED)

    def test_cannot_read_someone_elses_notification(self):
        theirs = self._make(self.bob)
        resp = self.client.post(f"{self.LIST}{theirs.id}/read/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_mark_all_read_only_touches_own_unread(self):
        a1 = self._make(self.alice)
        a2 = self._make(self.alice)
        b1 = self._make(self.bob)
        resp = self.client.post(f"{self.LIST}mark-all-read/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["updated"], 2)
        for n in (a1, a2):
            n.refresh_from_db()
            self.assertEqual(n.status, Notification.STATUS_READ)
        b1.refresh_from_db()
        self.assertEqual(b1.status, Notification.STATUS_UNREAD)


# ---------------------------------------------------------------------------
# Invitaciones pendientes de antes de tener cuenta (ver signals.py)
# ---------------------------------------------------------------------------
class InvitationNotificationSignalTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.workspace, user=self.owner, role=Membership.ROLE_OWNER)

    def test_registering_with_an_invited_email_creates_a_notification(self):
        invitation = Invitation.objects.create(
            workspace=self.workspace, email="nueva@example.com", invited_by=self.owner
        )
        resp = self.client.post(
            "/api/v1/auth/register/",
            {"username": "nueva", "email": "nueva@example.com", "password": "S3gura-pw-99"},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        user = User.objects.get(username="nueva")
        n = Notification.objects.get(user=user, kind=Notification.KIND_INVITATION)
        self.assertEqual(n.status, Notification.STATUS_UNREAD)
        self.assertEqual(n.related_object_id, str(invitation.id))
        self.assertEqual(n.data["invitation_id"], str(invitation.id))

    def test_registering_with_no_invitation_creates_none(self):
        self.client.post(
            "/api/v1/auth/register/",
            {"username": "nueva", "email": "sin-invitacion@example.com", "password": "S3gura-pw-99"},
        )
        user = User.objects.get(username="nueva")
        self.assertFalse(Notification.objects.filter(user=user).exists())

    def test_accepting_the_invitation_resolves_the_notification(self):
        invitation = Invitation.objects.create(
            workspace=self.workspace, email="nueva@example.com", invited_by=self.owner
        )
        self.client.post(
            "/api/v1/auth/register/",
            {"username": "nueva", "email": "nueva@example.com", "password": "S3gura-pw-99"},
        )
        user = User.objects.get(username="nueva")
        self.client.force_authenticate(user)

        resp = self.client.post(f"/api/v1/invitations/{invitation.token}/accept/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        n = Notification.objects.get(user=user, kind=Notification.KIND_INVITATION)
        self.assertEqual(n.status, Notification.STATUS_RESOLVED)
