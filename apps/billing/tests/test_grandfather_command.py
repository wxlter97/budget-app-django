"""`manage.py grandfather_existing_users` -- ver la docstring del comando
para el porqué."""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import PROVIDER_MANUAL, Plan, Subscription

User = get_user_model()


def make_user(username, *, days_ago=0):
    user = User.objects.create_user(username, f"{username}@example.com", "pw")
    if days_ago:
        user.date_joined = timezone.now() - timedelta(days=days_ago)
        user.save(update_fields=["date_joined"])
    return user


class GrandfatherCommandTests(TestCase):
    def setUp(self):
        self.free = Plan.objects.create(code="free", name="Gratis", is_default=True)
        self.pro = Plan.objects.create(code="pro", name="Pro")

    def test_errors_without_a_pro_plan(self):
        Plan.objects.all().delete()
        with self.assertRaises(CommandError):
            call_command("grandfather_existing_users")

    def test_dry_run_creates_nothing(self):
        make_user("alice", days_ago=30)
        call_command("grandfather_existing_users", "--dry-run", stdout=StringIO())
        self.assertEqual(Subscription.objects.count(), 0)

    def test_grants_pro_to_existing_users(self):
        alice = make_user("alice", days_ago=30)
        call_command("grandfather_existing_users", stdout=StringIO())

        sub = Subscription.objects.get(user=alice)
        self.assertEqual(sub.plan, self.pro)
        self.assertEqual(sub.status, Subscription.STATUS_ACTIVE)
        self.assertEqual(sub.provider, PROVIDER_MANUAL)
        self.assertIsNone(sub.current_period_end)
        self.assertIn("Grandfathered", sub.notes)

    def test_users_created_after_the_cutoff_are_not_grandfathered(self):
        make_user("bob")  # date_joined = ahora
        cutoff = (timezone.now() - timedelta(days=1)).isoformat()
        call_command("grandfather_existing_users", f"--before={cutoff}", stdout=StringIO())
        self.assertEqual(Subscription.objects.count(), 0)

    def test_users_with_an_active_subscription_are_skipped(self):
        carol = make_user("carol", days_ago=30)
        Subscription.objects.create(user=carol, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        call_command("grandfather_existing_users", stdout=StringIO())
        # No se creó una segunda de más -- sigue habiendo sólo la que ya tenía.
        self.assertEqual(Subscription.objects.filter(user=carol).count(), 1)

    def test_is_idempotent(self):
        make_user("dave", days_ago=30)
        call_command("grandfather_existing_users", stdout=StringIO())
        call_command("grandfather_existing_users", stdout=StringIO())
        self.assertEqual(Subscription.objects.count(), 1)
