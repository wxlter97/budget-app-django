"""POST /auth/me/delete/ -- ver `apps.users.services.delete_own_account`."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.workspaces.models import Membership, Workspace

User = get_user_model()
DELETE = "/api/v1/auth/me/delete/"


class DeleteOwnSoloWorkspaceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "S3gura-pw-99")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.user)

    def test_wrong_password_is_rejected(self):
        resp = self.client.post(DELETE, {"password": "incorrecta"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_correct_password_deletes_the_user_and_their_solo_workspace(self):
        ws_id = self.ws.id
        resp = self.client.post(DELETE, {"password": "S3gura-pw-99"})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.data)
        self.assertFalse(User.objects.filter(username="alice").exists())
        self.assertFalse(Workspace.objects.filter(id=ws_id).exists())


class DeleteGoogleOnlyAccountTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("bob", "bob@example.com")
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_authenticate(self.user)

    def test_requires_confirm_true_instead_of_password(self):
        resp = self.client.post(DELETE, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        resp = self.client.post(DELETE, {"confirm": True})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.data)
        self.assertFalse(User.objects.filter(username="bob").exists())


class DeleteSharedWorkspaceOwnerTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@example.com", "S3gura-pw-99")
        self.member = User.objects.create_user("member", "m@example.com", "S3gura-pw-99")
        self.ws = Workspace.objects.create(name="Compartido")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)
        self.client.force_authenticate(self.owner)

    def test_owner_of_a_shared_workspace_cannot_delete_yet(self):
        resp = self.client.post(DELETE, {"password": "S3gura-pw-99"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Compartido", str(resp.data))
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        self.assertTrue(Workspace.objects.filter(pk=self.ws.pk).exists())


class DeleteAsPlainMemberTests(APITestCase):
    """Ser miembro (no owner) de un workspace compartido no bloquea nada --
    borrar la cuenta simplemente la saca de ese workspace."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@example.com", "S3gura-pw-99")
        self.member = User.objects.create_user("member", "m@example.com", "S3gura-pw-99")
        self.ws = Workspace.objects.create(name="Compartido")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)
        self.client.force_authenticate(self.member)

    def test_member_can_delete_and_workspace_survives(self):
        resp = self.client.post(DELETE, {"password": "S3gura-pw-99"})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.data)
        self.assertFalse(User.objects.filter(username="member").exists())
        self.assertTrue(Workspace.objects.filter(pk=self.ws.pk).exists())
        self.assertFalse(Membership.objects.filter(workspace=self.ws, user_id=self.member.pk).exists())
