"""Comando `seed_categories`."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.transactions.models import Category
from apps.workspaces.models import Membership, Workspace

User = get_user_model()


class SeedCategoriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws_a = Workspace.objects.create(name="A")
        cls.ws_b = Workspace.objects.create(name="B")
        for ws in (cls.ws_a, cls.ws_b):
            Membership.objects.create(workspace=ws, user=cls.user, role=Membership.ROLE_OWNER)

    def _run(self, *args):
        call_command("seed_categories", *args, stdout=StringIO())

    def test_seeds_all_workspaces(self):
        self._run()
        self.assertTrue(Category.objects.filter(workspace=self.ws_a).exists())
        self.assertTrue(Category.objects.filter(workspace=self.ws_b).exists())
        self.assertTrue(
            Category.objects.filter(workspace=self.ws_a, type=Category.TYPE_INCOME).exists()
        )
        self.assertTrue(
            Category.objects.filter(workspace=self.ws_a, type=Category.TYPE_EXPENSE).exists()
        )

    def test_creates_group_hierarchy(self):
        self._run("--workspace", str(self.ws_a.id))
        groups = Category.objects.filter(workspace=self.ws_a, parent__isnull=True)
        children = Category.objects.filter(workspace=self.ws_a, parent__isnull=False)
        self.assertGreater(groups.count(), 1)
        self.assertGreater(children.count(), groups.count())
        # todo hijo cuelga de un grupo (2 niveles como máximo)
        self.assertFalse(
            children.filter(parent__parent__isnull=False).exists()
        )

    def test_is_idempotent(self):
        self._run("--workspace", str(self.ws_a.id))
        count = Category.objects.filter(workspace=self.ws_a).count()
        self._run("--workspace", str(self.ws_a.id))
        self.assertEqual(Category.objects.filter(workspace=self.ws_a).count(), count)

    def test_workspace_filter(self):
        self._run("--workspace", str(self.ws_a.id))
        self.assertTrue(Category.objects.filter(workspace=self.ws_a).exists())
        self.assertFalse(Category.objects.filter(workspace=self.ws_b).exists())

    def test_only_empty_skips_populated(self):
        Category.objects.create(
            workspace=self.ws_a, name="Custom", type=Category.TYPE_EXPENSE
        )
        self._run("--only-empty")
        self.assertEqual(Category.objects.filter(workspace=self.ws_a).count(), 1)
        self.assertTrue(Category.objects.filter(workspace=self.ws_b).exists())
