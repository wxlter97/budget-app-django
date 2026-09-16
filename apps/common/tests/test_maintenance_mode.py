"""Interruptor de emergencia `MAINTENANCE_MODE` (ver middleware.py / RUNBOOK.md §8)."""
from django.test import RequestFactory, TestCase, override_settings

from apps.common.middleware import MaintenanceModeMiddleware


class MaintenanceModeMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = MaintenanceModeMiddleware(lambda request: "ok")

    def test_off_by_default(self):
        resp = self.client.get("/healthz/")
        self.assertEqual(resp.status_code, 200)

    @override_settings(MAINTENANCE_MODE=True, MAINTENANCE_MESSAGE="Volvemos pronto.")
    def test_blocks_api_when_enabled(self):
        resp = self.client.get("/api/v1/wallets/")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "Volvemos pronto.")

    @override_settings(MAINTENANCE_MODE=True)
    def test_healthz_stays_up_when_enabled(self):
        resp = self.client.get("/healthz/")
        self.assertEqual(resp.status_code, 200)

    @override_settings(MAINTENANCE_MODE=True)
    def test_admin_prefix_is_exempt(self):
        request = self.factory.get("/admin/login/")
        response = self.middleware(request)
        self.assertEqual(response, "ok")

    @override_settings(MAINTENANCE_MODE=True)
    def test_everything_else_is_blocked(self):
        request = self.factory.get("/api/v1/transactions/")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 503)
