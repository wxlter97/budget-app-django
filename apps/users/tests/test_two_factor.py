"""2FA por TOTP: setup -> enable -> login pide el segundo paso -> verify."""
import pyotp
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.users.models import TwoFactorAuth

User = get_user_model()

TOKEN = "/api/v1/auth/token/"
STATUS_URL = "/api/v1/auth/2fa/"
SETUP = "/api/v1/auth/2fa/setup/"
ENABLE = "/api/v1/auth/2fa/enable/"
DISABLE = "/api/v1/auth/2fa/disable/"
BACKUP_CODES = "/api/v1/auth/2fa/backup-codes/"
VERIFY = "/api/v1/auth/2fa/verify/"

PASSWORD = "S3gura-pw-99"


class TwoFactorSetupTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", PASSWORD)
        self.client.force_authenticate(self.user)

    def test_requires_authentication(self):
        self.client.force_authenticate(None)
        resp = self.client.post(SETUP)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_setup_creates_a_pending_secret(self):
        resp = self.client.post(SETUP)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertIn("secret", resp.data)
        self.assertIn("otpauth_url", resp.data)
        self.assertIn(resp.data["secret"], resp.data["otpauth_url"])
        two_factor = TwoFactorAuth.objects.get(user=self.user)
        self.assertFalse(two_factor.enabled)

    def test_status_is_disabled_before_enabling(self):
        resp = self.client.get(STATUS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["enabled"])

    def test_enable_with_correct_code_activates_and_returns_backup_codes(self):
        setup = self.client.post(SETUP).data
        code = pyotp.TOTP(setup["secret"]).now()
        resp = self.client.post(ENABLE, {"code": code})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["enabled"])
        self.assertEqual(len(resp.data["backup_codes"]), TwoFactorAuth.BACKUP_CODES_COUNT)
        self.assertEqual(len(set(resp.data["backup_codes"])), TwoFactorAuth.BACKUP_CODES_COUNT)

        two_factor = TwoFactorAuth.objects.get(user=self.user)
        self.assertTrue(two_factor.enabled)
        self.assertIsNotNone(two_factor.confirmed_at)

        status_resp = self.client.get(STATUS_URL)
        self.assertTrue(status_resp.data["enabled"])

    def test_enable_with_wrong_code_fails(self):
        self.client.post(SETUP)
        resp = self.client.post(ENABLE, {"code": "000000"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(TwoFactorAuth.objects.get(user=self.user).enabled)

    def test_enable_without_prior_setup_fails(self):
        resp = self.client.post(ENABLE, {"code": "123456"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_setup_blocked_while_already_enabled(self):
        setup = self.client.post(SETUP).data
        code = pyotp.TOTP(setup["secret"]).now()
        self.client.post(ENABLE, {"code": code})

        resp = self.client.post(SETUP)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TwoFactorLoginFlowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("bob", "b@example.com", PASSWORD)

    def _enable_2fa(self):
        self.client.force_authenticate(self.user)
        setup = self.client.post(SETUP).data
        secret = setup["secret"]
        code = pyotp.TOTP(secret).now()
        enable = self.client.post(ENABLE, {"code": code}).data
        self.client.force_authenticate(None)
        return secret, enable["backup_codes"]

    def test_login_without_2fa_returns_tokens_directly(self):
        resp = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)
        self.assertNotIn("two_factor_required", resp.data)

    def test_wrong_password_is_unaffected_by_2fa(self):
        self._enable_2fa()
        resp = self.client.post(TOKEN, {"username": "bob", "password": "not-it"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_with_2fa_asks_for_second_step_without_issuing_tokens(self):
        self._enable_2fa()
        resp = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["two_factor_required"])
        self.assertIn("mfa_token", resp.data)
        self.assertNotIn("access", resp.data)
        self.assertNotIn("refresh", resp.data)

    def test_mfa_token_cannot_be_used_as_a_real_access_token(self):
        secret, _ = self._enable_2fa()
        mfa_token = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        resp = self.client.get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {mfa_token}"
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_verify_with_correct_totp_returns_tokens(self):
        secret, _ = self._enable_2fa()
        mfa_token = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        code = pyotp.TOTP(secret).now()
        resp = self.client.post(VERIFY, {"mfa_token": mfa_token, "code": code})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        self.assertEqual(resp.data["user"]["username"], "bob")

    def test_verify_with_wrong_code_fails(self):
        self._enable_2fa()
        mfa_token = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        resp = self.client.post(VERIFY, {"mfa_token": mfa_token, "code": "000000"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_with_garbage_mfa_token_fails(self):
        resp = self.client.post(VERIFY, {"mfa_token": "not-a-real-token", "code": "123456"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_with_backup_code_works_once(self):
        _, backup_codes = self._enable_2fa()
        mfa_token = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        first_use = self.client.post(VERIFY, {"mfa_token": mfa_token, "code": backup_codes[0]})
        self.assertEqual(first_use.status_code, status.HTTP_200_OK, first_use.data)

        # El mismo código de respaldo, en un segundo login, ya no sirve.
        mfa_token_2 = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        second_use = self.client.post(VERIFY, {"mfa_token": mfa_token_2, "code": backup_codes[0]})
        self.assertEqual(second_use.status_code, status.HTTP_400_BAD_REQUEST)

    def test_backup_code_is_case_insensitive(self):
        _, backup_codes = self._enable_2fa()
        mfa_token = self.client.post(TOKEN, {"username": "bob", "password": PASSWORD}).data["mfa_token"]
        resp = self.client.post(VERIFY, {"mfa_token": mfa_token, "code": backup_codes[0].lower()})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class TwoFactorDisableTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("carol", "c@example.com", PASSWORD)
        self.client.force_authenticate(self.user)
        setup = self.client.post(SETUP).data
        self.secret = setup["secret"]
        code = pyotp.TOTP(self.secret).now()
        self.client.post(ENABLE, {"code": code})

    def test_disable_requires_correct_password(self):
        resp = self.client.post(DISABLE, {"password": "wrong-one"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(TwoFactorAuth.objects.get(user=self.user).enabled)

    def test_disable_with_correct_password_turns_it_off(self):
        resp = self.client.post(DISABLE, {"password": PASSWORD})
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        two_factor = TwoFactorAuth.objects.get(user=self.user)
        self.assertFalse(two_factor.enabled)
        self.assertEqual(two_factor.backup_codes, [])

    def test_login_is_normal_again_after_disabling(self):
        self.client.post(DISABLE, {"password": PASSWORD})
        self.client.force_authenticate(None)
        resp = self.client.post(TOKEN, {"username": "carol", "password": PASSWORD})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)

    def test_can_set_up_again_after_disabling(self):
        self.client.post(DISABLE, {"password": PASSWORD})
        resp = self.client.post(SETUP)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class TwoFactorBackupCodesRegenerationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("dave", "d@example.com", PASSWORD)
        self.client.force_authenticate(self.user)
        setup = self.client.post(SETUP).data
        self.secret = setup["secret"]
        code = pyotp.TOTP(self.secret).now()
        self.original_codes = self.client.post(ENABLE, {"code": code}).data["backup_codes"]

    def test_regenerate_requires_password(self):
        resp = self.client.post(BACKUP_CODES, {"password": "wrong"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_regenerate_invalidates_old_codes(self):
        resp = self.client.post(BACKUP_CODES, {"password": PASSWORD})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        new_codes = resp.data["backup_codes"]
        self.assertEqual(len(new_codes), TwoFactorAuth.BACKUP_CODES_COUNT)
        self.assertNotEqual(set(new_codes), set(self.original_codes))

        self.client.force_authenticate(None)
        mfa_token = self.client.post(TOKEN, {"username": "dave", "password": PASSWORD}).data["mfa_token"]
        old_code_resp = self.client.post(VERIFY, {"mfa_token": mfa_token, "code": self.original_codes[0]})
        self.assertEqual(old_code_resp.status_code, status.HTTP_400_BAD_REQUEST)

        mfa_token_2 = self.client.post(TOKEN, {"username": "dave", "password": PASSWORD}).data["mfa_token"]
        new_code_resp = self.client.post(VERIFY, {"mfa_token": mfa_token_2, "code": new_codes[0]})
        self.assertEqual(new_code_resp.status_code, status.HTTP_200_OK)
