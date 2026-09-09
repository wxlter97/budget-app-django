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
from apps.notifications.models import NotificationLog, NotificationPreference, PushDevice
from apps.transactions.models import Category, CategoryBudget, RecurringExpense, Transaction
from apps.workspaces.models import Membership, Workspace

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
    def test_no_reminder_when_due_date_is_further_out(self, mock_send):
        self.recurring.next_due_date = timezone.localdate() + timedelta(days=5)
        self.recurring.save(update_fields=["next_due_date"])
        services.notify_due_items()
        mock_send.assert_not_called()


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
            month=today.month, year=today.year,
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
