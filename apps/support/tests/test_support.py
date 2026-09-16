from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.support.models import SupportTicket, SupportTicketMessage
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class SupportTicketApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.other_user = User.objects.create_user("bob", "b@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=cls.ws, user=cls.other_user, role=Membership.ROLE_MEMBER)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    @patch("apps.support.services.requests.post")
    def test_create_ticket_notifies_discord(self, mock_post):
        with override_settings(SUPPORT_WEBHOOK_URL="https://discord.com/api/webhooks/x/y"):
            resp = self.client.post(
                "/api/v1/support-tickets/",
                {
                    "type": SupportTicket.TYPE_BUG,
                    "subject": "El botón de borrar no hace nada",
                    "message": "Lo toco y no pasa nada, en iOS.",
                    "app_version": "1.2.3",
                    "platform": "ios",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        ticket = SupportTicket.objects.get(id=resp.data["id"])
        self.assertEqual(ticket.created_by, self.user)
        self.assertEqual(ticket.workspace, self.ws)
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertIn("El botón de borrar no hace nada", kwargs["json"]["content"])

    @patch("apps.support.services.requests.post")
    def test_no_webhook_configured_skips_notification(self, mock_post):
        with override_settings(SUPPORT_WEBHOOK_URL=""):
            resp = self.client.post(
                "/api/v1/support-tickets/",
                {"type": SupportTicket.TYPE_QUERY, "subject": "¿Cómo cambio de moneda?", "message": "..."},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        mock_post.assert_not_called()

    @patch(
        "apps.support.services.requests.post",
        side_effect=requests.RequestException("boom"),
    )
    def test_discord_failure_does_not_break_ticket_creation(self, mock_post):
        with override_settings(SUPPORT_WEBHOOK_URL="https://discord.com/api/webhooks/x/y"):
            resp = self.client.post(
                "/api/v1/support-tickets/",
                {"type": SupportTicket.TYPE_BUG, "subject": "x", "message": "y"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SupportTicket.objects.count(), 1)

    def test_status_is_read_only_on_create(self):
        resp = self.client.post(
            "/api/v1/support-tickets/",
            {
                "type": SupportTicket.TYPE_BUG, "subject": "x", "message": "y",
                "status": SupportTicket.STATUS_RESOLVED,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["status"], SupportTicket.STATUS_OPEN)

    def test_user_only_sees_own_tickets(self):
        SupportTicket.objects.create(
            workspace=self.ws, created_by=self.other_user,
            type=SupportTicket.TYPE_BUG, subject="Ajeno", message="...",
        )
        SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user,
            type=SupportTicket.TYPE_BUG, subject="Mío", message="...",
        )
        resp = self.client.get("/api/v1/support-tickets/")
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(resp.data["results"][0]["subject"], "Mío")

    def test_reply_adds_message_to_thread(self):
        ticket = SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user,
            type=SupportTicket.TYPE_BUG, subject="x", message="y",
        )
        resp = self.client.post(
            f"/api/v1/support-tickets/{ticket.id}/reply/",
            {"message": "Ah, se me olvidó decir que pasa sólo en Android."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(len(resp.data["messages"]), 1)
        self.assertFalse(resp.data["messages"][0]["is_staff_reply"])

        msg = SupportTicketMessage.objects.get(ticket=ticket)
        self.assertEqual(msg.author, self.user)
        self.assertFalse(msg.is_staff_reply)

    def test_staff_reply_is_flagged_as_such(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        ticket = SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user,
            type=SupportTicket.TYPE_BUG, subject="x", message="y",
        )
        resp = self.client.post(
            f"/api/v1/support-tickets/{ticket.id}/reply/",
            {"message": "Ya lo arreglamos, actualizá la app."},
            format="json",
        )
        self.assertTrue(resp.data["messages"][0]["is_staff_reply"])

    def test_reply_rejects_blank_message(self):
        ticket = SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user,
            type=SupportTicket.TYPE_BUG, subject="x", message="y",
        )
        resp = self.client.post(
            f"/api/v1/support-tickets/{ticket.id}/reply/", {"message": "   "}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_reply_to_another_users_ticket(self):
        ticket = SupportTicket.objects.create(
            workspace=self.ws, created_by=self.other_user,
            type=SupportTicket.TYPE_BUG, subject="x", message="y",
        )
        resp = self.client.post(
            f"/api/v1/support-tickets/{ticket.id}/reply/", {"message": "hola"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_filter_by_status_and_type(self):
        SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user, type=SupportTicket.TYPE_BUG,
            subject="bug abierto", message="...", status=SupportTicket.STATUS_OPEN,
        )
        SupportTicket.objects.create(
            workspace=self.ws, created_by=self.user, type=SupportTicket.TYPE_QUERY,
            subject="consulta resuelta", message="...", status=SupportTicket.STATUS_RESOLVED,
        )
        resp = self.client.get("/api/v1/support-tickets/?status=resolved")
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(resp.data["results"][0]["subject"], "consulta resuelta")

        resp = self.client.get("/api/v1/support-tickets/?type=bug")
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(resp.data["results"][0]["subject"], "bug abierto")
