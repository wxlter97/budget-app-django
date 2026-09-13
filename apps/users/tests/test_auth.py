from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()

REGISTER = "/api/v1/auth/register/"
ME = "/api/v1/auth/me/"
TOKEN = "/api/v1/auth/token/"
PASSWORD_CHANGE = "/api/v1/auth/password/change/"
PASSWORD_SET = "/api/v1/auth/password/set/"


class RegisterTests(APITestCase):
    def test_register_creates_user_and_returns_tokens(self):
        resp = self.client.post(
            REGISTER,
            {"username": "nueva", "email": "nueva@example.com", "password": "S3gura-pw-99"},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        self.assertNotIn("password", resp.data["user"])
        user = User.objects.get(username="nueva")
        self.assertTrue(user.check_password("S3gura-pw-99"))

    def test_register_starts_onboarding_pending(self):
        # A diferencia de una cuenta que ya existía antes de este campo
        # (default=True en la columna), una cuenta que se crea ACÁ es
        # realmente nueva -- tiene que ver el tour de bienvenida.
        self.client.post(
            REGISTER,
            {"username": "nueva", "email": "nueva@example.com", "password": "S3gura-pw-99"},
        )
        user = User.objects.get(username="nueva")
        self.assertFalse(user.onboarding_completed)

    def test_duplicate_email_is_rejected_case_insensitive(self):
        User.objects.create_user("existente", "dup@example.com", "x")
        resp = self.client.post(
            REGISTER,
            {"username": "otra", "email": "DUP@example.com", "password": "S3gura-pw-99"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", resp.data)

    def test_duplicate_username_is_rejected(self):
        User.objects.create_user("taken", "a@example.com", "x")
        resp = self.client.post(
            REGISTER,
            {"username": "taken", "email": "b@example.com", "password": "S3gura-pw-99"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", resp.data)

    def test_weak_password_is_rejected(self):
        resp = self.client.post(
            REGISTER,
            {"username": "debil", "email": "debil@example.com", "password": "123"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", resp.data)

    def test_can_obtain_token_after_register(self):
        self.client.post(
            REGISTER,
            {"username": "loginable", "email": "l@example.com", "password": "S3gura-pw-99"},
        )
        resp = self.client.post(TOKEN, {"username": "loginable", "password": "S3gura-pw-99"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)


class MeTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "yo", "yo@example.com", "pw", first_name="Yo"
        )

    def test_me_requires_authentication(self):
        self.assertEqual(self.client.get(ME).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_own_data(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get(ME)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["email"], "yo@example.com")
        self.assertEqual(resp.data["username"], "yo")
        self.assertFalse(resp.data["two_factor_enabled"])
        self.assertTrue(resp.data["has_password"])
        # Creada directo con `create_user` (no vía /auth/register/): cuenta
        # "de antes", no debe quedar pidiendo el tour de bienvenida.
        self.assertTrue(resp.data["onboarding_completed"])

    def test_me_reports_two_factor_enabled(self):
        from apps.users.models import TwoFactorAuth

        TwoFactorAuth.objects.create(user=self.user, enabled=True)
        self.client.force_authenticate(self.user)
        resp = self.client.get(ME)
        self.assertTrue(resp.data["two_factor_enabled"])

    def test_me_can_mark_onboarding_completed(self):
        self.user.onboarding_completed = False
        self.user.save(update_fields=["onboarding_completed"])
        self.client.force_authenticate(self.user)
        resp = self.client.patch(ME, {"onboarding_completed": True})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.onboarding_completed)

    def test_me_can_update_names_and_email(self):
        self.client.force_authenticate(self.user)
        resp = self.client.patch(ME, {"first_name": "Walter", "email": "walter@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Walter")
        self.assertEqual(self.user.email, "walter@example.com")

    def test_me_username_is_read_only(self):
        self.client.force_authenticate(self.user)
        self.client.patch(ME, {"username": "otro"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "yo")

    def test_me_rejects_email_taken_by_another_user(self):
        User.objects.create_user("otro", "ocupado@example.com", "pw")
        self.client.force_authenticate(self.user)
        resp = self.client.patch(ME, {"email": "ocupado@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ChangePasswordTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("yo", "yo@example.com", "S3gura-pw-99")
        self.client.force_authenticate(self.user)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(
            PASSWORD_CHANGE, {"current_password": "S3gura-pw-99", "new_password": "Otra-pw-88"}
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_changes_password_with_correct_current_password(self):
        resp = self.client.post(
            PASSWORD_CHANGE, {"current_password": "S3gura-pw-99", "new_password": "Otra-pw-88"}
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Otra-pw-88"))

    def test_rejects_wrong_current_password(self):
        resp = self.client.post(
            PASSWORD_CHANGE, {"current_password": "mala", "new_password": "Otra-pw-88"}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", resp.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("S3gura-pw-99"))

    def test_rejects_weak_new_password(self):
        resp = self.client.post(
            PASSWORD_CHANGE, {"current_password": "S3gura-pw-99", "new_password": "123"}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", resp.data)

    def test_google_only_account_must_use_set_endpoint(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        resp = self.client.post(
            PASSWORD_CHANGE, {"current_password": "x", "new_password": "Otra-pw-88"}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class SetPasswordTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "soloGoogle", "google@example.com", "irrelevante"
        )
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.client.force_authenticate(self.user)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(PASSWORD_SET, {"new_password": "Otra-pw-88"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sets_password_on_google_only_account(self):
        resp = self.client.post(PASSWORD_SET, {"new_password": "Otra-pw-88"})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT, resp.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Otra-pw-88"))
        self.assertTrue(self.user.has_usable_password())

    def test_rejects_weak_password(self):
        resp = self.client.post(PASSWORD_SET, {"new_password": "123"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_account_with_password_already_must_use_change_endpoint(self):
        self.user.set_password("Ya-tengo-pw-1")
        self.user.save(update_fields=["password"])
        resp = self.client.post(PASSWORD_SET, {"new_password": "Otra-pw-88"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
