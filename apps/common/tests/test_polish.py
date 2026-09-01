"""
Throttling, verificación de firma del webhook y header del esquema OpenAPI.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from apps.email_import.models import BankEmailSchema
from apps.workspaces.models import Workspace

User = get_user_model()

# DRF fija THROTTLE_RATES como atributo de clase al importar; en tests hay que
# parchearlo además de usar @override_settings.
TEST_RATES = {"anon": "40/min", "user": "1000/hour", "auth": "3/min", "inbound": "2/min"}


def _with_throttling():
    return mock.patch.object(SimpleRateThrottle, "THROTTLE_RATES", TEST_RATES)


class ThrottlingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self._patcher = _with_throttling()
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        cache.clear()

    def test_login_endpoint_is_throttled(self):
        User.objects.create_user("u", "u@example.com", "pw")
        for _ in range(3):  # scope auth = 3/min
            self.client.post("/api/v1/auth/token/", {"username": "u", "password": "x"})
        resp = self.client.post("/api/v1/auth/token/", {"username": "u", "password": "x"})
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_register_endpoint_is_throttled(self):
        for i in range(3):
            self.client.post(
                "/api/v1/auth/register/",
                {"username": f"u{i}", "email": f"u{i}@x.com", "password": "S3gura-pw-99"},
            )
        resp = self.client.post(
            "/api/v1/auth/register/",
            {"username": "u9", "email": "u9@x.com", "password": "S3gura-pw-99"},
        )
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


@override_settings(INBOUND_WEBHOOK_SECRET="shh", INBOUND_MAILGUN_SIGNING_KEY="mg-key")
class WebhookAuthTests(APITestCase):
    URL = "/api/v1/email-import/inbound/"

    def setUp(self):
        cache.clear()
        self.ws = Workspace.objects.create(name="A")
        BankEmailSchema.objects.create(bank_name="Demo Bank", sender_pattern=r"@demobank\.com")

    def _to(self):
        return f"import+{self.ws.inbound_token}@inbound.budget.local"

    def _mailgun_sig(self, ts="1700000000", token="abc123"):
        import hashlib
        import hmac

        return ts, token, hmac.new(
            b"mg-key", f"{ts}{token}".encode(), hashlib.sha256
        ).hexdigest()

    def test_mailgun_hmac_accepted(self):
        ts, token, sig = self._mailgun_sig()
        resp = self.client.post(
            self.URL,
            {
                "to": self._to(), "from": "alertas@demobank.com",
                "timestamp": ts, "token": token, "signature": sig, "text": "x",
            },
        )
        self.assertEqual(resp.status_code, 202, resp.data)

    def test_mailgun_bad_hmac_rejected(self):
        resp = self.client.post(
            self.URL,
            {
                "to": self._to(), "from": "alertas@demobank.com",
                "timestamp": "1700000000", "token": "abc123", "signature": "deadbeef",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_shared_secret_still_works(self):
        resp = self.client.post(
            self.URL,
            {"to": self._to(), "from": "alertas@demobank.com", "text": "x"},
            HTTP_X_INBOUND_SECRET="shh",
        )
        self.assertEqual(resp.status_code, 202, resp.data)


class OpenApiSchemaTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(User.objects.create_superuser("admin", "a@x.com", "pw"))

    def test_workspace_header_documented_only_where_needed(self):
        paths = self.client.get("/api/schema/").data["paths"]

        def names(path, method):
            return [p["name"] for p in paths[path][method].get("parameters", [])]

        self.assertIn("X-Workspace-ID", names("/api/v1/wallets/", "get"))
        self.assertIn("X-Workspace-ID", names("/api/v1/reports/net-worth/", "get"))
        self.assertNotIn("X-Workspace-ID", names("/api/v1/auth/register/", "post"))
        self.assertNotIn("X-Workspace-ID", names("/api/v1/workspaces/", "get"))
