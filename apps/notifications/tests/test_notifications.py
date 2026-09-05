from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
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
