"""Capa 4: "Continuar con Google". No pegamos de verdad a Google en tests --
se mockea `verify_google_id_token` (la función que sí hace la llamada real,
cubierta aparte en test_google_service.py)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.users.services import GoogleTokenError

User = get_user_model()
GOOGLE = "/api/v1/auth/google/"

CLAIMS = {
    "email": "nueva@example.com",
    "email_verified": "true",
    "given_name": "Nueva",
    "family_name": "Persona",
    "picture": "https://lh3.googleusercontent.com/foto.jpg",
    "aud": "client-id",
}


class GoogleLoginTests(APITestCase):
    @patch("apps.users.api.verify_google_id_token")
    def test_creates_new_user_on_first_login(self, verify):
        verify.return_value = CLAIMS
        resp = self.client.post(GOOGLE, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["created"])
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

        user = User.objects.get(email="nueva@example.com")
        self.assertEqual(user.first_name, "Nueva")
        self.assertEqual(user.last_name, "Persona")
        self.assertEqual(user.profile_photo_url, CLAIMS["picture"])
        self.assertFalse(user.has_usable_password())

    @patch("apps.users.api.verify_google_id_token")
    def test_second_login_reuses_same_account(self, verify):
        verify.return_value = CLAIMS
        self.client.post(GOOGLE, {"id_token": "fake"})
        self.assertEqual(User.objects.filter(email="nueva@example.com").count(), 1)

        resp = self.client.post(GOOGLE, {"id_token": "fake-again"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["created"])
        self.assertEqual(User.objects.filter(email="nueva@example.com").count(), 1)

    @patch("apps.users.api.verify_google_id_token")
    def test_rejects_existing_password_account_without_link(self, verify):
        # Google solo sirve para CREAR cuentas -- una cuenta usuario/contraseña
        # que nunca vinculó Google no debe poder entrar así, o cualquier
        # correo de Google ajeno bastaría para entrar a esa cuenta.
        existing = User.objects.create_user("nueva", "nueva@example.com", "S3gura-pw-99")
        verify.return_value = CLAIMS
        resp = self.client.post(GOOGLE, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("id_token", resp.data)
        existing.refresh_from_db()
        self.assertEqual(existing.profile_photo_url, "")

    @patch("apps.users.api.verify_google_id_token")
    def test_logs_into_previously_linked_account(self, verify):
        existing = User.objects.create_user(
            "nueva", "nueva@example.com", "S3gura-pw-99", google_linked=True,
        )
        verify.return_value = CLAIMS
        resp = self.client.post(GOOGLE, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["created"])
        self.assertEqual(resp.data["user"]["id"], existing.id)
        existing.refresh_from_db()
        self.assertTrue(existing.has_usable_password())
        self.assertEqual(existing.profile_photo_url, CLAIMS["picture"])

    @patch("apps.users.api.verify_google_id_token")
    def test_does_not_overwrite_existing_photo(self, verify):
        existing = User.objects.create_user(
            "nueva", "nueva@example.com", "S3gura-pw-99", google_linked=True,
            profile_photo_url="https://misfoto.example/yo.png",
        )
        verify.return_value = CLAIMS
        self.client.post(GOOGLE, {"id_token": "fake"})
        existing.refresh_from_db()
        self.assertEqual(existing.profile_photo_url, "https://misfoto.example/yo.png")

    @patch("apps.users.api.verify_google_id_token")
    def test_invalid_token_returns_400(self, verify):
        verify.side_effect = GoogleTokenError("Token de Google inválido o expirado.")
        resp = self.client.post(GOOGLE, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("id_token", resp.data)

    def test_missing_id_token_returns_400(self):
        resp = self.client.post(GOOGLE, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class GoogleLinkTests(APITestCase):
    """Herramientas → Cuenta → "Vincular cuenta de Google": habilita a una
    cuenta usuario/contraseña a entrar con Google, sin poder crear otra."""

    LINK = "/api/v1/auth/google/link/"

    def setUp(self):
        self.user = User.objects.create_user("nueva", "nueva@example.com", "S3gura-pw-99")
        self.client.force_authenticate(self.user)

    @patch("apps.users.api.verify_google_id_token")
    def test_links_when_email_matches(self, verify):
        verify.return_value = CLAIMS
        resp = self.client.post(self.LINK, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["google_linked"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.google_linked)
        self.assertEqual(self.user.profile_photo_url, CLAIMS["picture"])
        # sigue pudiendo entrar con su contraseña de siempre
        self.assertTrue(self.user.has_usable_password())

    @patch("apps.users.api.verify_google_id_token")
    def test_rejects_mismatched_email(self, verify):
        verify.return_value = {**CLAIMS, "email": "otra@example.com"}
        resp = self.client.post(self.LINK, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.google_linked)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(self.LINK, {"id_token": "fake"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.users.api.verify_google_id_token")
    def test_linked_account_can_then_login_with_google(self, verify):
        verify.return_value = CLAIMS
        self.client.post(self.LINK, {"id_token": "fake"})
        resp = self.client.post(GOOGLE, {"id_token": "fake-again"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["created"])
        self.assertEqual(resp.data["user"]["id"], self.user.id)
