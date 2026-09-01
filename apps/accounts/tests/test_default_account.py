"""Wallet.is_default: máx. una por workspace."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class DefaultWalletTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _create(self, name, is_default=False):
        return self.client.post(
            "/api/v1/wallets/",
            {"name": name, "purpose": "spending", "is_default": is_default},
            **{HEADER: str(self.ws.id)},
        )

    def test_marking_new_default_unsets_previous(self):
        r1 = self._create("A", is_default=True)
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        r2 = self._create("B", is_default=True)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)

        defaults = Wallet.objects.filter(workspace=self.ws, is_default=True)
        self.assertEqual(defaults.count(), 1)
        self.assertEqual(defaults.first().name, "B")

    def test_can_move_default_by_patch(self):
        a = Wallet.objects.create(
            workspace=self.ws, name="A", purpose="spending", is_default=True
        )
        b = Wallet.objects.create(workspace=self.ws, name="B", purpose="spending")

        res = self.client.patch(
            f"/api/v1/wallets/{b.id}/", {"is_default": True}, **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_default)
        self.assertTrue(b.is_default)
