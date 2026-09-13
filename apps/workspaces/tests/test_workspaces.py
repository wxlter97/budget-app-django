"""POST /api/v1/workspaces/ -- alta de workspace."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.transactions.models import Category
from apps.workspaces.models import Membership, Workspace

User = get_user_model()


class WorkspaceCreateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)

    def test_creates_membership_as_owner(self):
        resp = self.client.post("/api/v1/workspaces/", {"name": "Casa"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        ws = Workspace.objects.get(id=resp.data["id"])
        membership = Membership.objects.get(workspace=ws, user=self.user)
        self.assertEqual(membership.role, Membership.ROLE_OWNER)

    def test_seeds_default_categories_so_it_never_starts_blank(self):
        # Antes, un workspace nuevo arrancaba sin ninguna categoría -- no
        # había ni dónde categorizar la primera transacción. Ver
        # `apps.transactions.services.seed_default_categories`.
        resp = self.client.post("/api/v1/workspaces/", {"name": "Casa"})
        ws = Workspace.objects.get(id=resp.data["id"])
        self.assertTrue(Category.objects.filter(workspace=ws, type=Category.TYPE_EXPENSE).exists())
        self.assertTrue(Category.objects.filter(workspace=ws, type=Category.TYPE_INCOME).exists())
        # 2 niveles: grupos (sin padre) con categorías asignables adentro.
        self.assertTrue(
            Category.objects.filter(workspace=ws, parent__isnull=True).exists()
        )
        self.assertTrue(
            Category.objects.filter(workspace=ws, parent__isnull=False).exists()
        )

    def test_seeding_is_scoped_to_the_new_workspace_only(self):
        other = Workspace.objects.create(name="Otro, sin categorías")
        self.client.post("/api/v1/workspaces/", {"name": "Casa"})
        self.assertFalse(Category.objects.filter(workspace=other).exists())


class WorkspaceDeleteTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_authenticate(self.user)
        self.only = Workspace.objects.create(name="Único")
        Membership.objects.create(workspace=self.only, user=self.user, role=Membership.ROLE_OWNER)

    def test_cannot_delete_only_workspace(self):
        resp = self.client.delete(f"/api/v1/workspaces/{self.only.id}/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.only.refresh_from_db()
        self.assertFalse(self.only.is_deleted)

    def test_can_delete_when_another_workspace_remains(self):
        second = Workspace.objects.create(name="Segundo")
        Membership.objects.create(workspace=second, user=self.user, role=Membership.ROLE_OWNER)
        resp = self.client.delete(f"/api/v1/workspaces/{self.only.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.only.refresh_from_db()
        self.assertTrue(self.only.is_deleted)

    def test_member_cannot_delete_workspace_regardless(self):
        second = Workspace.objects.create(name="Segundo")
        Membership.objects.create(workspace=second, user=self.user, role=Membership.ROLE_OWNER)
        member = User.objects.create_user("bob", "bob@example.com", "pw")
        Membership.objects.create(workspace=self.only, user=member, role=Membership.ROLE_MEMBER)
        self.client.force_authenticate(member)
        resp = self.client.delete(f"/api/v1/workspaces/{self.only.id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
