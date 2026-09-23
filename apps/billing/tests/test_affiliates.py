"""Influencers: atribución por enlace `?ref=` o código, y comisión en cada pago."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.billing.models import (
    Affiliate,
    AffiliateReferral,
    Payment,
    Plan,
    PlanPrice,
    PromoCode,
    Subscription,
)
from apps.billing.providers import WebhookEvent
from apps.billing.services import apply_webhook_event, redeem_promo_code

User = get_user_model()
REGISTER = "/api/v1/auth/register/"
GOOGLE = "/api/v1/auth/google/"


def make_affiliate(code="ANA30", **kw):
    plus, _ = Plan.objects.get_or_create(code="plus", defaults={"name": "Plus"})
    affiliate = Affiliate.objects.create(name="Ana", **kw)
    promo = PromoCode.objects.create(code=code, plan=plus, duration_days=30, affiliate=affiliate)
    return affiliate, promo


class SignupAttributionTests(APITestCase):
    def _register(self, **extra):
        body = {"username": "nuevo", "email": "nuevo@example.com", "password": "Una-clave-larga-123"}
        body.update(extra)
        resp = self.client.post(REGISTER, body)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return User.objects.get(username="nuevo")

    def test_registering_from_an_influencer_link_attributes_and_grants_the_benefit(self):
        affiliate, promo = make_affiliate()
        user = self._register(ref="ana30")

        referral = AffiliateReferral.objects.get(user=user)
        self.assertEqual(referral.affiliate, affiliate)
        self.assertEqual(referral.source, AffiliateReferral.SOURCE_LINK)
        sub = Subscription.objects.get(user=user)
        self.assertEqual(sub.plan.code, "plus")
        self.assertTrue(sub.is_in_force)

    def test_an_unknown_or_non_influencer_ref_is_ignored_without_failing_the_signup(self):
        plus = Plan.objects.create(code="plus", name="Plus")
        PromoCode.objects.create(code="BETA", plan=plus)  # código normal, sin afiliado
        user = self._register(ref="BETA")
        self.assertFalse(AffiliateReferral.objects.filter(user=user).exists())
        self.assertFalse(Subscription.objects.filter(user=user).exists())

    def test_an_exhausted_code_still_attributes_but_gives_no_benefit(self):
        affiliate, promo = make_affiliate(code="ANA1")
        promo.max_redemptions = 0
        promo.save()
        user = self._register(ref="ANA1")
        self.assertEqual(AffiliateReferral.objects.get(user=user).affiliate, affiliate)
        self.assertFalse(Subscription.objects.filter(user=user).exists())

    @patch("apps.users.api.verify_google_id_token")
    def test_google_signup_with_ref_is_attributed_only_when_the_account_is_new(self, verify):
        verify.return_value = {"email": "g@example.com", "email_verified": "true", "aud": "x"}
        affiliate, _ = make_affiliate()
        resp = self.client.post(GOOGLE, {"id_token": "fake", "ref": "ANA30"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        user = User.objects.get(email="g@example.com")
        self.assertEqual(AffiliateReferral.objects.get(user=user).affiliate, affiliate)

        other, _ = make_affiliate(code="OTRO")
        self.client.post(GOOGLE, {"id_token": "fake", "ref": "OTRO"})  # ya existía: no cambia nada
        self.assertEqual(AffiliateReferral.objects.get(user=user).affiliate, affiliate)


class CodeAttributionTests(TestCase):
    def test_redeeming_an_influencer_code_attributes_the_user(self):
        affiliate, _ = make_affiliate()
        user = User.objects.create_user("u", "u@example.com", "pw")
        redeem_promo_code(user, "ANA30")
        referral = AffiliateReferral.objects.get(user=user)
        self.assertEqual((referral.affiliate, referral.source), (affiliate, AffiliateReferral.SOURCE_CODE))

    def test_first_contact_wins(self):
        first, _ = make_affiliate(code="PRIMERO")
        make_affiliate(code="SEGUNDO")
        user = User.objects.create_user("u", "u@example.com", "pw")
        AffiliateReferral.objects.create(user=user, affiliate=first, source=AffiliateReferral.SOURCE_LINK)
        redeem_promo_code(user, "SEGUNDO")
        self.assertEqual(AffiliateReferral.objects.get(user=user).affiliate, first)


class CommissionTests(TestCase):
    def setUp(self):
        self.affiliate, _ = make_affiliate(commission_percent=Decimal("20"), commission_months=6)
        self.user = User.objects.create_user("u", "u@example.com", "pw")
        AffiliateReferral.objects.create(
            user=self.user, affiliate=self.affiliate, source=AffiliateReferral.SOURCE_LINK
        )
        pro = Plan.objects.create(code="pro", name="Pro")
        self.price = PlanPrice.objects.create(plan=pro, billing_period=PlanPrice.BILLING_MONTHLY, amount_cents=199)

    def _pay(self, user=None, tx="tx-1", amount="1.99"):
        sub = Subscription.objects.create(
            user=user or self.user, plan=self.price.plan, plan_price=self.price, provider="wompi"
        )
        apply_webhook_event(
            WebhookEvent(kind="subscription.activated", reference=str(sub.checkout_reference),
                         event_id=tx, amount=Decimal(amount)),
            provider_code="wompi",
        )
        return Payment.objects.get(event_id=tx)

    def test_a_payment_from_a_referred_user_records_the_commission(self):
        payment = self._pay()
        self.assertEqual(payment.amount_cents, 199)
        self.assertEqual(payment.affiliate, self.affiliate)
        self.assertEqual(payment.commission_cents, 39)  # 20 % de 1.99, redondeado hacia abajo

    def test_payments_of_users_without_referral_have_no_commission(self):
        other = User.objects.create_user("o", "o@example.com", "pw")
        payment = self._pay(user=other, tx="tx-o")
        self.assertIsNone(payment.affiliate)
        self.assertEqual(payment.commission_cents, 0)

    def test_no_commission_after_the_commission_window(self):
        first = self._pay(tx="tx-1")
        first.paid_at = timezone.now() - timedelta(days=200)
        first.save()
        later = self._pay(tx="tx-2")
        self.assertEqual(later.commission_cents, 0)

    def test_an_inactive_affiliate_earns_no_new_commission(self):
        self.affiliate.is_active = False
        self.affiliate.save()
        self.assertEqual(self._pay().commission_cents, 0)

    def test_a_repeated_webhook_records_a_single_payment(self):
        sub = Subscription.objects.create(
            user=self.user, plan=self.price.plan, plan_price=self.price, provider="wompi"
        )
        event = WebhookEvent(kind="subscription.activated", reference=str(sub.checkout_reference),
                             event_id="tx-dup", amount=Decimal("1.99"))
        apply_webhook_event(event, provider_code="wompi")
        apply_webhook_event(event, provider_code="wompi")
        self.assertEqual(Payment.objects.filter(event_id="tx-dup").count(), 1)


# El manifest de estáticos sólo existe tras `collectstatic`: para renderizar el admin en
# tests alcanza el storage simple.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AffiliateAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)

    def test_affiliate_and_payment_lists_load_with_their_numbers(self):
        affiliate, _ = make_affiliate()
        user = User.objects.create_user("u", "u@example.com", "pw")
        AffiliateReferral.objects.create(user=user, affiliate=affiliate, source="link")
        pro = Plan.objects.create(code="pro", name="Pro")
        price = PlanPrice.objects.create(plan=pro, billing_period=PlanPrice.BILLING_MONTHLY, amount_cents=1000)
        sub = Subscription.objects.create(user=user, plan=pro, plan_price=price, provider="wompi")
        Payment.objects.create(user=user, subscription=sub, provider="wompi", amount_cents=1000,
                               affiliate=affiliate, commission_cents=200)

        resp = self.client.get("/admin/billing/affiliate/")
        self.assertContains(resp, "10.00")  # facturado
        self.assertContains(resp, "2.00")   # comisión pendiente
        self.assertEqual(self.client.get("/admin/billing/payment/?commission_paid=no").status_code, 200)

    def test_marking_commissions_as_paid(self):
        affiliate, _ = make_affiliate()
        user = User.objects.create_user("u", "u@example.com", "pw")
        pro = Plan.objects.create(code="pro", name="Pro")
        sub = Subscription.objects.create(user=user, plan=pro, provider="wompi")
        payment = Payment.objects.create(user=user, subscription=sub, provider="wompi", amount_cents=1000,
                                         affiliate=affiliate, commission_cents=200)
        self.client.post("/admin/billing/payment/", {
            "action": "mark_commission_paid", "_selected_action": [str(payment.pk)],
        })
        payment.refresh_from_db()
        self.assertIsNotNone(payment.commission_paid_at)
