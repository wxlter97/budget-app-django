"""Gestión de miembros: volver a sumar a alguien que se fue, salir de un
presupuesto y administrar las invitaciones pendientes desde adentro."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.workspaces.models import Invitation, Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"
MEMBERSHIPS = "/api/v1/memberships/"
LEAVE = "/api/v1/memberships/leave/"
WS_INVITATIONS = "/api/v1/workspace-invitations/"


def make_workspace(owner, name="Casa"):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class BaseMembersTest(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.ws = make_workspace(self.owner)
        # Cada uno con su propio presupuesto, como deja el onboarding.
        make_workspace(self.owner, "Alice personal")
        make_workspace(self.bob, "Bob personal")

    def as_user(self, user):
        self.client.force_authenticate(user)
        self.client.credentials(**{HEADER: str(self.ws.id)})


class ReAddRemovedMemberTests(BaseMembersTest):
    def test_owner_can_re_add_someone_they_removed(self):
        m = Membership.objects.create(workspace=self.ws, user=self.bob)
        self.as_user(self.owner)
        self.assertEqual(self.client.delete(f"{MEMBERSHIPS}{m.id}/").status_code, 204)

        resp = self.client.post(MEMBERSHIPS, {"email": "bob@example.com"})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(Membership.objects.filter(workspace=self.ws, user=self.bob).count(), 1)

    def test_invitation_accept_revives_membership_of_someone_who_left(self):
        Membership.all_objects.create(workspace=self.ws, user=self.bob, is_deleted=True)
        inv = Invitation.objects.create(
            workspace=self.ws, email="bob@example.com", invited_by=self.owner
        )
        self.client.force_authenticate(self.bob)

        resp = self.client.post(f"/api/v1/invitations/{inv.token}/accept/")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(Membership.objects.filter(workspace=self.ws, user=self.bob).exists())


class LeaveWorkspaceTests(BaseMembersTest):
    def test_member_can_leave(self):
        Membership.objects.create(workspace=self.ws, user=self.bob)
        self.as_user(self.bob)

        resp = self.client.post(LEAVE)

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Membership.objects.filter(workspace=self.ws, user=self.bob).exists())
        # Y ya no puede usar el workspace.
        self.assertEqual(self.client.get(MEMBERSHIPS).status_code, 403)

    def test_last_owner_with_members_must_name_another_owner(self):
        Membership.objects.create(workspace=self.ws, user=self.bob)
        self.as_user(self.owner)

        resp = self.client.post(LEAVE)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("otro dueño", str(resp.data))

    def test_owner_can_leave_when_another_owner_remains(self):
        Membership.objects.create(workspace=self.ws, user=self.bob, role=Membership.ROLE_OWNER)
        self.as_user(self.owner)

        self.assertEqual(self.client.post(LEAVE).status_code, 204)

    def test_alone_in_workspace_cannot_leave(self):
        self.as_user(self.owner)
        resp = self.client.post(LEAVE)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("borralo", str(resp.data))

    def test_cannot_leave_only_workspace(self):
        carol = User.objects.create_user("carol", "carol@example.com", "pw")
        Membership.objects.create(workspace=self.ws, user=carol)
        self.as_user(carol)

        resp = self.client.post(LEAVE)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("único presupuesto", str(resp.data))


class WorkspaceInvitationsTests(BaseMembersTest):
    def setUp(self):
        super().setUp()
        self.inv = Invitation.objects.create(
            workspace=self.ws, email="nuevo@example.com", invited_by=self.owner
        )
        other_ws = make_workspace(self.bob, "Ajeno")
        Invitation.objects.create(workspace=other_ws, email="x@example.com", invited_by=self.bob)

    def test_lists_only_pending_invitations_of_this_workspace(self):
        Invitation.objects.create(
            workspace=self.ws, email="viejo@example.com", status=Invitation.STATUS_ACCEPTED
        )
        self.as_user(self.owner)

        resp = self.client.get(WS_INVITATIONS)

        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        self.assertEqual([i["email"] for i in results], ["nuevo@example.com"])

    def test_owner_can_cancel_and_link_stops_working(self):
        self.as_user(self.owner)

        self.assertEqual(self.client.delete(f"{WS_INVITATIONS}{self.inv.id}/").status_code, 204)

        self.client.credentials()
        self.assertEqual(self.client.get(f"/api/v1/invitations/{self.inv.token}/").status_code, 404)
        # Y se puede volver a invitar a ese correo.
        self.as_user(self.owner)
        self.assertEqual(self.client.post(MEMBERSHIPS, {"email": "nuevo@example.com"}).status_code, 202)

    def test_member_can_see_but_not_cancel(self):
        Membership.objects.create(workspace=self.ws, user=self.bob)
        self.as_user(self.bob)

        self.assertEqual(self.client.get(WS_INVITATIONS).status_code, 200)
        self.assertEqual(self.client.delete(f"{WS_INVITATIONS}{self.inv.id}/").status_code, 403)

    def test_resend_sends_the_email_again_with_cooldown(self):
        Invitation.objects.filter(pk=self.inv.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )
        self.as_user(self.owner)

        first = self.client.post(f"{WS_INVITATIONS}{self.inv.id}/resend/")
        second = self.client.post(f"{WS_INVITATIONS}{self.inv.id}/resend/")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.inv.token, mail.outbox[0].body)
        self.assertEqual(second.status_code, 400)

    def test_cannot_touch_invitations_of_another_workspace(self):
        foreign = Invitation.objects.get(email="x@example.com")
        self.as_user(self.owner)
        with patch("apps.workspaces.api.send_invitation_email") as send:
            resp = self.client.post(f"{WS_INVITATIONS}{foreign.id}/resend/")
        self.assertEqual(resp.status_code, 404)
        send.assert_not_called()
