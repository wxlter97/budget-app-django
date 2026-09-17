from django.core.management import call_command
from django.test import TestCase

from apps.billing.models import Plan, PlanPrice


class SeedBillingPlansTests(TestCase):
    def test_creates_free_default_plus_and_pro_with_their_prices(self):
        call_command("seed_billing_plans")

        free = Plan.objects.get(code="free")
        plus = Plan.objects.get(code="plus")
        pro = Plan.objects.get(code="pro")
        self.assertTrue(free.is_default)
        self.assertFalse(plus.is_default)
        self.assertFalse(pro.is_default)
        self.assertEqual(free.max_workspaces_owned, 1)
        self.assertEqual(plus.max_workspaces_owned, 2)
        self.assertIsNone(pro.max_workspaces_owned)

        plus_prices = {p.billing_period: p.amount for p in plus.prices.all()}
        self.assertEqual(plus_prices, {
            PlanPrice.BILLING_MONTHLY: 0.99,
            PlanPrice.BILLING_ANNUAL: 9.99,
        })

        pro_prices = {p.billing_period: p.amount for p in pro.prices.all()}
        self.assertEqual(pro_prices, {
            PlanPrice.BILLING_MONTHLY: 1.99,
            PlanPrice.BILLING_ANNUAL: 14.99,
            PlanPrice.BILLING_LIFETIME: 19.99,
        })

    def test_is_idempotent(self):
        call_command("seed_billing_plans")
        call_command("seed_billing_plans")
        self.assertEqual(Plan.objects.count(), 3)
        self.assertEqual(PlanPrice.objects.count(), 5)
