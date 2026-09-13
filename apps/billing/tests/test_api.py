from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.billing.models import Plan, PlanPrice, Subscription

User = get_user_model()

PLANS = "/api/v1/plans/"
ME = "/api/v1/billing/me/"
CHECKOUT = "/api/v1/billing/checkout/"
CANCEL = "/api/v1/billing/cancel/"
WEBHOOK_WOMPI = "/api/v1/billing/webhooks/wompi/"


def make_plans_with_prices():
    free = Plan.objects.create(code="free", name="Gratis", is_default=True, max_workspaces_owned=1)
    pro = Plan.objects.create(code="pro", name="Pro", max_workspaces_owned=None)
    monthly = PlanPrice.objects.create(
        plan=pro, billing_period=PlanPrice.BILLING_MONTHLY, amount_cents=199, currency="USD",
    )
    return free, pro, monthly


class PlanCatalogTests(APITestCase):
    def setUp(self):
        self.free, self.pro, self.monthly = make_plans_with_prices()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_lists_plans_with_their_active_prices(self):
        resp = self.client.get(PLANS)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        pro_row = next(p for p in resp.data["results"] if p["code"] == "pro")
        self.assertEqual(len(pro_row["prices"]), 1)
        self.assertAlmostEqual(pro_row["prices"][0]["amount"], 1.99)

    def test_inactive_prices_are_not_listed(self):
        self.monthly.is_active = False
        self.monthly.save()
        resp = self.client.get(PLANS)
        pro_row = next(p for p in resp.data["results"] if p["code"] == "pro")
        self.assertEqual(pro_row["prices"], [])

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.get(PLANS)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class MyPlanViewTests(APITestCase):
    def setUp(self):
        self.free, self.pro, self.monthly = make_plans_with_prices()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_no_subscription_returns_default_plan_and_null_subscription(self):
        resp = self.client.get(ME)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["plan"]["code"], "free")
        self.assertIsNone(resp.data["subscription"])

    def test_active_subscription_returns_its_plan_and_billing_period(self):
        Subscription.objects.create(
            user=self.user, plan=self.pro, plan_price=self.monthly, status=Subscription.STATUS_ACTIVE,
        )
        resp = self.client.get(ME)
        self.assertEqual(resp.data["plan"]["code"], "pro")
        self.assertEqual(resp.data["subscription"]["billing_period"], "monthly")


class CheckoutViewTests(APITestCase):
    def setUp(self):
        self.free, self.pro, self.monthly = make_plans_with_prices()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def _checkout(self, **overrides):
        body = {
            "plan_price": str(self.monthly.id),
            "success_url": "https://app.example.com/pro/ok",
            "cancel_url": "https://app.example.com/pro/cancel",
        }
        body.update(overrides)
        return self.client.post(CHECKOUT, body)

    def test_price_without_provider_ref_is_rejected(self):
        # `self.monthly` no tiene `external_refs["wompi"]` cargado todavía.
        resp = self._checkout()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plan_price", resp.data)
        # No debe quedar una Subscription huérfana en `pending`.
        self.assertFalse(Subscription.objects.exists())

    def test_wompi_checkout_fails_loudly_while_unimplemented(self):
        self.monthly.external_refs = {"wompi": "plan_test_123"}
        self.monthly.save()
        resp = self._checkout(provider="wompi")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Subscription.objects.exists())

    def test_manual_provider_has_no_checkout(self):
        resp = self._checkout(provider="manual")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_provider_is_rejected(self):
        self.monthly.external_refs = {"wompi": "plan_test_123"}
        self.monthly.save()
        resp = self._checkout(provider="stripe")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class CancelSubscriptionViewTests(APITestCase):
    def setUp(self):
        self.free, self.pro, self.monthly = make_plans_with_prices()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_no_active_subscription_returns_404(self):
        resp = self.client.post(CANCEL)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_manual_subscription_can_be_canceled(self):
        Subscription.objects.create(
            user=self.user, plan=self.pro, provider=Subscription._meta.get_field("provider").default,
            status=Subscription.STATUS_ACTIVE,
        )
        resp = self.client.post(CANCEL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["status"], "canceled")

    def test_wompi_subscription_cancel_fails_loudly_while_unimplemented(self):
        Subscription.objects.create(
            user=self.user, plan=self.pro, provider="wompi", status=Subscription.STATUS_ACTIVE,
        )
        resp = self.client.post(CANCEL)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class WompiWebhookViewTests(APITestCase):
    def test_fails_with_501_while_signature_verification_is_unimplemented(self):
        resp = self.client.post(WEBHOOK_WOMPI, {"anything": "goes"}, format="json")
        self.assertEqual(resp.status_code, 501)
