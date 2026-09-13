from django.core.management import call_command
from django.test import TestCase

from apps.billing.models import Plan, PlanPrice


class SeedBillingPlansTests(TestCase):
    def test_creates_free_default_and_pro_with_three_prices(self):
        call_command("seed_billing_plans")

        free = Plan.objects.get(code="free")
        pro = Plan.objects.get(code="pro")
        self.assertTrue(free.is_default)
        self.assertFalse(pro.is_default)
        self.assertEqual(free.max_workspaces_owned, 1)
        self.assertIsNone(pro.max_workspaces_owned)

        prices = {p.billing_period: p.amount for p in pro.prices.all()}
        self.assertEqual(prices, {
            PlanPrice.BILLING_MONTHLY: 1.99,
            PlanPrice.BILLING_ANNUAL: 19.99,
            PlanPrice.BILLING_LIFETIME: 19.99,
        })

    def test_is_idempotent(self):
        call_command("seed_billing_plans")
        call_command("seed_billing_plans")
        self.assertEqual(Plan.objects.count(), 2)
        self.assertEqual(PlanPrice.objects.count(), 3)
