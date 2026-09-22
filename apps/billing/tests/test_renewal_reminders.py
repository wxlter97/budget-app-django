"""send_renewal_reminders: avisos de vencimiento y detección de "no se renovó"
(Wompi nunca avisa un cobro fallido -- ver WompiProvider)."""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Plan, PlanPrice, Subscription
from apps.billing.services import notify_billing_event, send_renewal_reminders
from apps.notifications.models import Notification, PushDevice

User = get_user_model()


def make_sub(period, *, amount_cents=99, when, status=Subscription.STATUS_ACTIVE, **kw):
    user = kw.pop("user", None) or User.objects.create_user(
        f"u{Subscription.objects.count()}", f"u{Subscription.objects.count()}@example.com", "pw"
    )
    plan, _ = Plan.objects.get_or_create(code="pro", defaults={"name": "Pro"})
    price = PlanPrice.objects.create(plan=plan, billing_period=period, amount_cents=amount_cents)
    return Subscription.objects.create(
        user=user, plan=plan, plan_price=price, provider="wompi",
        status=status, current_period_end=when, **kw,
    )


class ReminderWindowTests(TestCase):
    def test_a_monthly_plan_close_to_renewing_gets_a_reminder(self):
        sub = make_sub(PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=2))
        send_renewal_reminders()
        n = Notification.objects.get(user=sub.user)
        self.assertEqual(n.kind, Notification.KIND_SUBSCRIPTION_RENEWAL_DUE)
        self.assertIn("renueva", n.title.lower())
        sub.refresh_from_db()
        self.assertEqual(sub.renewal_notice_sent_for, sub.current_period_end)
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)  # sin cambios, sigue activa

    def test_an_annual_plan_close_to_expiring_gets_a_different_wording(self):
        sub = make_sub(PlanPrice.BILLING_ANNUAL, when=timezone.now() + dt.timedelta(days=1))
        send_renewal_reminders()
        n = Notification.objects.get(user=sub.user)
        self.assertEqual(n.kind, Notification.KIND_SUBSCRIPTION_RENEWAL_DUE)
        self.assertIn("vence", n.title.lower())
        self.assertIn("Renovalo", n.body)

    def test_far_from_expiring_gets_nothing(self):
        make_sub(PlanPrice.BILLING_ANNUAL, when=timezone.now() + dt.timedelta(days=30))
        send_renewal_reminders()
        self.assertFalse(Notification.objects.exists())

    def test_lifetime_is_never_reminded(self):
        # current_period_end es None para de por vida -- no debería ni entrar al filtro.
        sub = make_sub(PlanPrice.BILLING_LIFETIME, when=None)
        sub.current_period_end = None
        sub.save()
        send_renewal_reminders()
        self.assertFalse(Notification.objects.exists())

    def test_a_subscription_without_plan_price_is_skipped_not_crashed(self):
        user = User.objects.create_user("np", "np@example.com", "pw")
        plan, _ = Plan.objects.get_or_create(code="pro", defaults={"name": "Pro"})
        Subscription.objects.create(
            user=user, plan=plan, plan_price=None, provider="manual",
            status=Subscription.STATUS_ACTIVE, current_period_end=timezone.now() + dt.timedelta(hours=1),
        )
        send_renewal_reminders()  # no debe tirar

    def test_running_twice_the_same_day_does_not_double_notify(self):
        make_sub(PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=1))
        send_renewal_reminders()
        send_renewal_reminders()
        self.assertEqual(Notification.objects.count(), 1)

    def test_sends_a_push_if_the_user_has_a_device(self):
        sub = make_sub(PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=1))
        PushDevice.objects.create(user=sub.user, token="ExponentPushToken[x]", platform="ios")
        with mock.patch("apps.notifications.services.send_push") as push:
            send_renewal_reminders()
        push.assert_called_once()


class ExpiryDetectionTests(TestCase):
    def test_an_active_sub_past_its_period_end_is_marked_expired(self):
        sub = make_sub(PlanPrice.BILLING_ANNUAL, when=timezone.now() - dt.timedelta(hours=1))
        send_renewal_reminders()
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_EXPIRED)
        n = Notification.objects.get(user=sub.user)
        self.assertEqual(n.kind, Notification.KIND_SUBSCRIPTION_EXPIRED)
        self.assertIn("venció", n.title)

    def test_a_past_due_sub_past_its_period_end_is_also_expired(self):
        sub = make_sub(
            PlanPrice.BILLING_MONTHLY, when=timezone.now() - dt.timedelta(minutes=1),
            status=Subscription.STATUS_PAST_DUE,
        )
        send_renewal_reminders()
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_EXPIRED)

    def test_an_already_canceled_sub_is_left_alone(self):
        sub = make_sub(
            PlanPrice.BILLING_ANNUAL, when=timezone.now() - dt.timedelta(days=5),
            status=Subscription.STATUS_CANCELED,
        )
        send_renewal_reminders()
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.STATUS_CANCELED)
        self.assertFalse(Notification.objects.exists())

    def test_an_already_expired_sub_is_not_renotified(self):
        sub = make_sub(
            PlanPrice.BILLING_ANNUAL, when=timezone.now() - dt.timedelta(days=1),
            status=Subscription.STATUS_EXPIRED,
        )
        send_renewal_reminders()
        self.assertFalse(Notification.objects.exists())

    def test_renewing_after_a_reminder_allows_a_future_reminder_again(self):
        sub = make_sub(PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=1))
        send_renewal_reminders()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.renewal_notice_sent_for)
        # Se renueva: current_period_end avanza un mes.
        sub.current_period_end = timezone.now() + dt.timedelta(days=32)
        sub.save()
        send_renewal_reminders()  # todavía falta mucho, no debería avisar de nuevo
        self.assertEqual(Notification.objects.count(), 1)
        sub.current_period_end = timezone.now() + dt.timedelta(days=2)
        sub.save()
        send_renewal_reminders()
        self.assertEqual(Notification.objects.count(), 2)  # el aviso del período nuevo


@override_settings(BILLING_WEBHOOK_URL="https://discord.example/webhook")
class BillingDiscordNotifyTests(TestCase):
    def _sub(self):
        return make_sub(PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=1))

    def test_posts_to_discord_with_the_plan_and_amount(self):
        sub = self._sub()
        with mock.patch("apps.billing.services.requests.post") as post:
            notify_billing_event("new_subscriber", sub)
        content = post.call_args.kwargs["json"]["content"]
        self.assertIn("Nueva suscripción", content)
        self.assertIn(sub.plan.name, content)
        self.assertIn(sub.user.email, content)
        self.assertIn("0.99", content)

    def test_without_billing_webhook_url_does_nothing(self):
        sub = self._sub()
        with override_settings(BILLING_WEBHOOK_URL=""):
            with mock.patch("apps.billing.services.requests.post") as post:
                notify_billing_event("new_subscriber", sub)
        post.assert_not_called()

    def test_a_failed_post_does_not_raise(self):
        import requests
        sub = self._sub()
        with mock.patch("apps.billing.services.requests.post", side_effect=requests.RequestException("down")):
            notify_billing_event("new_subscriber", sub)  # no debe tirar

    def test_send_renewal_reminders_notifies_discord_on_expiry(self):
        sub = make_sub(PlanPrice.BILLING_ANNUAL, when=timezone.now() - dt.timedelta(hours=1))
        with mock.patch("apps.billing.services.requests.post") as post:
            send_renewal_reminders()
        content = post.call_args.kwargs["json"]["content"]
        self.assertIn("Venció sin renovarse", content)


@override_settings(BILLING_WEBHOOK_URL="https://discord.example/webhook")
class ApplyWebhookEventDiscordTests(TestCase):
    """apply_webhook_event distingue una suscripción nueva de una renovación al avisar
    a Discord -- ver apps.billing.tests.test_wompi para el resto de apply_webhook_event."""

    def _event(self, sub, tx):
        from apps.billing.providers import WebhookEvent
        return WebhookEvent(
            kind="subscription.activated", reference=str(sub.checkout_reference),
            event_id=tx, amount=Decimal(str(sub.plan_price.amount)),
        )

    def test_first_activation_is_a_new_subscriber(self):
        from apps.billing.services import apply_webhook_event
        sub = make_sub(PlanPrice.BILLING_MONTHLY, when=None, status=Subscription.STATUS_PENDING)
        with mock.patch("apps.billing.services.requests.post") as post:
            apply_webhook_event(self._event(sub, "tx-1"), provider_code="wompi")
        self.assertIn("Nueva suscripción", post.call_args.kwargs["json"]["content"])

    def test_a_later_renewal_of_an_already_active_sub_is_a_renewal_not_a_new_subscriber(self):
        from apps.billing.services import apply_webhook_event
        sub = make_sub(
            PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=10),
            status=Subscription.STATUS_ACTIVE,
        )
        with mock.patch("apps.billing.services.requests.post") as post:
            apply_webhook_event(self._event(sub, "tx-2"), provider_code="wompi")
        self.assertIn("Renovación cobrada", post.call_args.kwargs["json"]["content"])

    def test_a_failed_event_notifies_discord_too_even_though_wompi_never_sends_one(self):
        from apps.billing.services import apply_webhook_event
        from apps.billing.providers import WebhookEvent
        sub = make_sub(
            PlanPrice.BILLING_MONTHLY, when=timezone.now() + dt.timedelta(days=10),
            status=Subscription.STATUS_ACTIVE,
        )
        event = WebhookEvent(kind="subscription.failed", reference=str(sub.checkout_reference), event_id="tx-3")
        with mock.patch("apps.billing.services.requests.post") as post:
            apply_webhook_event(event, provider_code="wompi")
        self.assertIn("Cobro fallido", post.call_args.kwargs["json"]["content"])
