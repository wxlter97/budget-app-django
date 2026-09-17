"""Códigos de invitación: `services.redeem_promo_code` + `POST /billing/redeem/`."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.billing.models import Plan, PromoCode, PromoCodeRedemption, Subscription
from apps.billing.services import redeem_promo_code

User = get_user_model()

REDEEM = "/api/v1/billing/redeem/"


def make_plan(code="pro"):
    return Plan.objects.create(code=code, name=code.capitalize(), max_workspaces_owned=None)


class RedeemPromoCodeServiceTests(TestCase):
    def setUp(self):
        self.plan = make_plan()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_redeems_a_valid_code(self):
        PromoCode.objects.create(code="beta2026", plan=self.plan)

        sub = redeem_promo_code(self.user, "beta2026")

        self.assertEqual(sub.plan, self.plan)
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)
        self.assertEqual(sub.provider, "manual")
        self.assertIsNone(sub.current_period_end)
        self.assertTrue(PromoCodeRedemption.objects.filter(user=self.user).exists())

    def test_code_lookup_is_case_and_whitespace_insensitive(self):
        PromoCode.objects.create(code="BETA2026", plan=self.plan)

        sub = redeem_promo_code(self.user, "  beta2026  ")
        self.assertEqual(sub.plan, self.plan)

    def test_duration_days_sets_a_period_end(self):
        PromoCode.objects.create(code="TRIAL30", plan=self.plan, duration_days=30)

        before = timezone.now() + timedelta(days=30)
        sub = redeem_promo_code(self.user, "TRIAL30")
        after = timezone.now() + timedelta(days=30)

        self.assertIsNotNone(sub.current_period_end)
        self.assertTrue(before <= sub.current_period_end <= after)

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValidationError):
            redeem_promo_code(self.user, "NOEXISTE")

    def test_inactive_code_is_rejected(self):
        PromoCode.objects.create(code="OFF", plan=self.plan, is_active=False)
        with self.assertRaises(ValidationError):
            redeem_promo_code(self.user, "OFF")

    def test_expired_code_is_rejected(self):
        PromoCode.objects.create(
            code="VENCIDO", plan=self.plan, expires_at=timezone.now() - timedelta(days=1)
        )
        with self.assertRaises(ValidationError):
            redeem_promo_code(self.user, "VENCIDO")

    def test_code_at_max_redemptions_is_rejected(self):
        promo = PromoCode.objects.create(code="LIMITADO", plan=self.plan, max_redemptions=1)
        redeem_promo_code(self.user, "LIMITADO")
        bob = User.objects.create_user("bob", "bob@example.com", "pw")

        with self.assertRaises(ValidationError):
            redeem_promo_code(bob, "LIMITADO")

        promo.refresh_from_db()
        self.assertEqual(promo.redemption_count, 1)

    def test_user_can_only_ever_redeem_one_code(self):
        PromoCode.objects.create(code="UNO", plan=self.plan)
        PromoCode.objects.create(code="DOS", plan=self.plan)

        redeem_promo_code(self.user, "UNO")
        with self.assertRaises(ValidationError):
            redeem_promo_code(self.user, "DOS")


class RedeemPromoCodeApiTests(APITestCase):
    def setUp(self):
        self.plan = make_plan()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_redeems_and_returns_the_new_subscription(self):
        PromoCode.objects.create(code="BETA", plan=self.plan)

        resp = self.client.post(REDEEM, {"code": "beta"})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["plan"]["code"], "pro")
        self.assertEqual(resp.data["status"], "active")

    def test_invalid_code_returns_400(self):
        resp = self.client.post(REDEEM, {"code": "NOEXISTE"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(REDEEM, {"code": "BETA"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
