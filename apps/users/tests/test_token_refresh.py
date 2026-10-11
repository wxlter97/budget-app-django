import time

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

REFRESH = "/api/v1/auth/token/refresh/"
User = get_user_model()


class LateRotationRefreshTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("ana", "ana@example.com", "S3gura-pw-99")

    def _token(self, age_fraction):
        """Refresh con `age_fraction` de su vida ya consumida."""
        token = RefreshToken.for_user(self.user)
        life = token.lifetime.total_seconds()
        token.set_exp(from_time=None)
        token.payload["exp"] = int(time.time() + life * (1 - age_fraction))
        return token

    def test_fresh_refresh_is_not_rotated_and_can_be_reused(self):
        token = self._token(0.1)
        for _ in range(2):
            resp = self.client.post(REFRESH, {"refresh": str(token)})
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            self.assertIn("access", resp.data)
            self.assertNotIn("refresh", resp.data)

    def test_old_refresh_is_rotated_and_old_one_blacklisted(self):
        token = self._token(0.7)
        resp = self.client.post(REFRESH, {"refresh": str(token)})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertIn("refresh", resp.data)
        self.assertNotEqual(resp.data["refresh"], str(token))
        again = self.client.post(REFRESH, {"refresh": str(token)})
        self.assertEqual(again.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_cannot_refresh_even_when_fresh(self):
        token = self._token(0.1)
        self.user.is_active = False
        self.user.save()
        resp = self.client.post(REFRESH, {"refresh": str(token)})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
