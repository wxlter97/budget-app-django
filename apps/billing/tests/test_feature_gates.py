"""Gates de `Plan.features` conectados en otras apps -- ver
`services.require_feature_for_workspace` y la convención de claves en
`billing.models.Plan` / `seed_billing_plans`. Cada uno se prueba a nivel
HTTP contra el endpoint real (no solo la función de servicio) para no
depender de que el wiring en la otra app siga vivo."""
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.billing.models import Plan
from apps.billing.services import has_feature_for_workspace
from apps.email_import.models import EmailImportLog
from apps.transactions.models import Category
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
WORKSPACE_HEADER = "HTTP_X_WORKSPACE_ID"

ALL_FEATURES = [
    "import_email", "import_excel", "net_worth_history", "advanced_reports",
    "export", "backup", "loyalty", "multi_currency", "quick_add",
]


def make_plans(*, pro_features=True):
    free = Plan.objects.create(
        code="free", name="Gratis", is_default=True,
        features={key: False for key in ALL_FEATURES},
    )
    pro = Plan.objects.create(
        code="pro", name="Pro",
        features={key: pro_features for key in ALL_FEATURES},
    )
    return free, pro


class FeatureResolutionTests(APITestCase):
    def setUp(self):
        self.free, self.pro = make_plans()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)

    def test_free_workspace_has_no_features(self):
        for key in ALL_FEATURES:
            self.assertFalse(has_feature_for_workspace(self.ws, key), key)

    def test_pro_workspace_has_all_features(self):
        from apps.billing.models import Subscription

        Subscription.objects.create(user=self.user, plan=self.pro, status=Subscription.STATUS_ACTIVE)
        for key in ALL_FEATURES:
            self.assertTrue(has_feature_for_workspace(self.ws, key), key)

    def test_unknown_feature_key_is_false_not_a_crash(self):
        self.assertFalse(has_feature_for_workspace(self.ws, "algo-que-no-existe"))


class _WorkspaceGateTestCase(APITestCase):
    """Base: workspace con owner autenticado, sobre el plan que cada test
    subclass arme en `setUp`."""

    def setUp(self):
        self.free, self.pro = make_plans()
        self.owner = User.objects.create_user("owner", "o@example.com", "pw")
        self.ws = Workspace.objects.create(name="Casa", base_currency="USD")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.owner)
        self.headers = {WORKSPACE_HEADER: str(self.ws.id)}

    def upgrade_to_pro(self):
        from apps.billing.models import Subscription

        Subscription.objects.create(user=self.owner, plan=self.pro, status=Subscription.STATUS_ACTIVE)


class BackupRestoreGateTests(_WorkspaceGateTestCase):
    def test_free_cannot_backup(self):
        resp = self.client.get(f"/api/v1/workspaces/{self.ws.id}/backup/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        self.assertIn("Pro", str(resp.data))

    def test_free_cannot_restore(self):
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws.id}/restore/", {"confirm": True}, **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_backup(self):
        self.upgrade_to_pro()
        resp = self.client.get(f"/api/v1/workspaces/{self.ws.id}/backup/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class ExchangeRateGateTests(_WorkspaceGateTestCase):
    URL = "/api/v1/exchange-rates/"

    def test_free_cannot_add_exchange_rate(self):
        resp = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "1.10"}, **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_add_exchange_rate(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            self.URL, {"currency": "EUR", "rate_to_base": "1.10"}, **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class ReportsGateTests(_WorkspaceGateTestCase):
    def test_free_cannot_see_cashflow(self):
        resp = self.client.get("/api/v1/reports/cashflow/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_see_category_trends(self):
        resp = self.client.get("/api/v1/reports/category-trends/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_see_net_worth_history(self):
        resp = self.client.get("/api/v1/monthly-snapshots/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_dashboard_summary_still_works(self):
        # No gateado -- reporte core, no "avanzado".
        resp = self.client.get("/api/v1/reports/summary/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_free_scheduled_still_works(self):
        # No gateado -- lo usa el dashboard (recurrentes/cuotas próximas).
        resp = self.client.get("/api/v1/reports/scheduled/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_pro_can_see_cashflow(self):
        self.upgrade_to_pro()
        resp = self.client.get("/api/v1/reports/cashflow/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class EmailImportGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta", is_default=True)
        self.category = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.log = EmailImportLog.objects.create(
            workspace=self.ws, status=EmailImportLog.STATUS_PENDING,
            raw_email_subject="Compra", extracted_amount="10.00",
            extracted_merchant="Tienda", extracted_date="2026-01-15",
        )

    def test_free_cannot_confirm(self):
        resp = self.client.post(
            f"/api/v1/email-import-logs/{self.log.id}/confirm/",
            {"wallet": str(self.wallet.id), "category": str(self.category.id)},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, EmailImportLog.STATUS_PENDING)

    def test_pro_can_confirm(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            f"/api/v1/email-import-logs/{self.log.id}/confirm/",
            {"wallet": str(self.wallet.id), "category": str(self.category.id)},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class QuickAddGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta", is_default=True)

    def test_free_cannot_create_a_shortcut_token(self):
        resp = self.client.post(
            "/api/v1/personal-tokens/",
            {"name": "iPhone", "wallet": str(self.wallet.id)},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_create_a_shortcut_token(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            "/api/v1/personal-tokens/",
            {"name": "iPhone", "wallet": str(self.wallet.id)},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


def _xlsx_upload():
    wb = Workbook()
    wb.active.append(["fecha", "tipo", "categoria", "cartera", "cartera_destino", "monto", "nota", "cuenta_presupuesto"])
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile(
        "movimientos.xlsx", buffer.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class ExcelImportGateTests(_WorkspaceGateTestCase):
    URL = "/api/v1/transactions/import/"

    def test_free_cannot_import_excel(self):
        resp = self.client.post(
            self.URL, {"file": _xlsx_upload()}, format="multipart", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_import_excel(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            self.URL, {"file": _xlsx_upload()}, format="multipart", **self.headers
        )
        # Sin filas, el import "vacío" igual responde 200 -- lo que importa
        # acá es que pasó el gate, no el contenido del archivo.
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class LoyaltyGateTests(_WorkspaceGateTestCase):
    def test_free_cannot_list_earnings(self):
        resp = self.client.get("/api/v1/loyalty-earnings/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_see_summary(self):
        resp = self.client.get("/api/v1/loyalty-earnings/summary/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_list_earnings(self):
        self.upgrade_to_pro()
        resp = self.client.get("/api/v1/loyalty-earnings/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
