"""Invitar a alguien SIN cuenta todavía (Capa 4): en vez de rechazar con
400, se crea una Invitation y se le manda un correo con el enlace para
sumarse; para alguien que ya tiene cuenta, el flujo directo de siempre."""
from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework import status
from rest_framework.test import APITestCase

from apps.workspaces.models import Invitation, Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"
MEMBERSHIPS = "/api/v1/memberships/"
INVITATIONS = "/api/v1/invitations/"


def make_workspace(owner, name="Casa"):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class InviteExistingUserTests(APITestCase):
    """Comportamiento de siempre: si ya hay cuenta, se agrega directo."""

    def setUp(self):
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.other = User.objects.create_user("bob", "bob@example.com", "pw")
        self.ws = make_workspace(self.owner)
        self.client.force_authenticate(self.owner)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_existing_user_is_added_immediately_no_email(self):
        resp = self.client.post(MEMBERSHIPS, {"email": "bob@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(Membership.objects.filter(workspace=self.ws, user=self.other).exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Invitation.objects.count(), 0)

    def test_cannot_invite_already_member(self):
        Membership.objects.create(workspace=self.ws, user=self.other, role=Membership.ROLE_MEMBER)
        resp = self.client.post(MEMBERSHIPS, {"email": "bob@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", resp.data)

    def test_only_owner_can_invite(self):
        member = User.objects.create_user("carol", "carol@example.com", "pw")
        Membership.objects.create(workspace=self.ws, user=member, role=Membership.ROLE_MEMBER)
        self.client.force_authenticate(member)
        resp = self.client.post(MEMBERSHIPS, {"email": "someone@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class InviteNewUserTests(APITestCase):
    """El caso nuevo de Capa 4: la persona todavía no tiene cuenta."""

    def setUp(self):
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.ws = make_workspace(self.owner)
        self.client.force_authenticate(self.owner)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_invite_unknown_email_creates_pending_invitation_and_sends_mail(self):
        resp = self.client.post(MEMBERSHIPS, {"email": "nueva@example.com", "role": "member"})
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED, resp.data)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["email"], "nueva@example.com")
        self.assertNotIn("user", resp.data)  # no es una Membership

        invitation = Invitation.objects.get()
        self.assertEqual(invitation.workspace, self.ws)
        self.assertEqual(invitation.role, "member")
        self.assertEqual(invitation.invited_by, self.owner)

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn("nueva@example.com", sent.to)
        self.assertIn(invitation.token, sent.body)
        self.assertIn(self.ws.name, sent.subject)

    def test_reinviting_same_email_reuses_pending_invitation(self):
        first = self.client.post(MEMBERSHIPS, {"email": "nueva@example.com"}).data
        second = self.client.post(MEMBERSHIPS, {"email": "nueva@example.com"}).data
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)  # se reenvía el correo cada vez

    def test_invalid_email_is_rejected(self):
        resp = self.client.post(MEMBERSHIPS, {"email": "no-es-un-correo"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", resp.data)


class InvitationAcceptDeclineTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        self.ws = make_workspace(self.owner)
        self.invitation = Invitation.objects.create(
            workspace=self.ws, email="nueva@example.com",
            role=Membership.ROLE_MEMBER, invited_by=self.owner,
        )

    def test_list_returns_only_my_pending_invitations(self):
        invitee = User.objects.create_user("nueva", "nueva@example.com", "pw")
        other = User.objects.create_user("otra", "otra@example.com", "pw")

        self.client.force_authenticate(invitee)
        resp = self.client.get(INVITATIONS)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["email"], "nueva@example.com")

        self.client.force_authenticate(other)
        resp = self.client.get(INVITATIONS)
        self.assertEqual(len(resp.data["results"]), 0)

    def test_retrieve_by_token_is_public(self):
        resp = self.client.get(f"{INVITATIONS}{self.invitation.token}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["workspace_name"], self.ws.name)

    def test_accept_creates_membership(self):
        invitee = User.objects.create_user("nueva", "nueva@example.com", "pw")
        self.client.force_authenticate(invitee)
        resp = self.client.post(f"{INVITATIONS}{self.invitation.token}/accept/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["status"], "accepted")
        self.assertTrue(
            Membership.objects.filter(workspace=self.ws, user=invitee, role="member").exists()
        )

    def test_accept_wrong_email_is_forbidden(self):
        someone_else = User.objects.create_user("otra", "otra@example.com", "pw")
        self.client.force_authenticate(someone_else)
        resp = self.client.post(f"{INVITATIONS}{self.invitation.token}/accept/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Membership.objects.filter(workspace=self.ws, user=someone_else).exists())

    def test_accept_requires_authentication(self):
        resp = self.client.post(f"{INVITATIONS}{self.invitation.token}/accept/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_decline_marks_invitation_declined(self):
        invitee = User.objects.create_user("nueva", "nueva@example.com", "pw")
        self.client.force_authenticate(invitee)
        resp = self.client.post(f"{INVITATIONS}{self.invitation.token}/decline/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, "declined")
        self.assertFalse(Membership.objects.filter(workspace=self.ws, user=invitee).exists())

    def test_cannot_accept_twice(self):
        invitee = User.objects.create_user("nueva", "nueva@example.com", "pw")
        self.client.force_authenticate(invitee)
        self.client.post(f"{INVITATIONS}{self.invitation.token}/accept/")
        resp = self.client.post(f"{INVITATIONS}{self.invitation.token}/accept/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
