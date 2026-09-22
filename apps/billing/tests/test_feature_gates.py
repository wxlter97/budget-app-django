"""Gates de `Plan.features` conectados en otras apps -- ver
`services.require_feature_for_workspace` y la convención de claves en
`billing.models.Plan` / `seed_billing_plans`. Cada uno se prueba a nivel
HTTP contra el endpoint real (no solo la función de servicio) para no
depender de que el wiring en la otra app siga vivo."""
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.billing.models import Plan
from apps.billing.services import has_feature_for_workspace
from apps.common.models import ModuleFlag
from apps.email_import.models import EmailImportLog
from apps.loyalty.models import Bank, CardProduct, CategoryType, LoyaltyProgram
from apps.transactions.models import Category
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
WORKSPACE_HEADER = "HTTP_X_WORKSPACE_ID"

ALL_FEATURES = [
    "import_email", "import_excel", "net_worth_history", "advanced_reports",
    "export", "backup", "loyalty", "multi_currency", "quick_add",
    # Restricción del gratis del 22-sep-2026 (ver seed_billing_plans y
    # ECONOMIA-POR-PLAN.md) -- "calendar" y "transaction_duplicate" están acá
    # (para que FeatureResolutionTests las cubra) aunque no tengan gate de
    # backend propio, ver el comentario en seed_billing_plans.py.
    "calendar", "notifications", "wallet_split", "transaction_duplicate",
    "refunds", "split_categories", "split_people", "installments",
    "statements", "net_worth",
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

    def test_pro_blocked_by_module_flag(self):
        # El interruptor manual (ver `apps.common.models.ModuleFlag`) manda
        # aparte del plan -- ni pagando se puede confirmar mientras está
        # apagado a mano.
        self.upgrade_to_pro()
        # `update_or_create` porque `common.0002_seed_module_flags` ya
        # sembró esta fila (habilitada) -- ver esa migración.
        ModuleFlag.objects.update_or_create(
            key="email_import", defaults={"label": "Importación por correo", "is_enabled": False}
        )
        resp = self.client.post(
            f"/api/v1/email-import-logs/{self.log.id}/confirm/",
            {"wallet": str(self.wallet.id), "category": str(self.category.id)},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE, resp.data)


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

    def test_pro_blocked_by_module_flag(self):
        self.upgrade_to_pro()
        ModuleFlag.objects.update_or_create(
            key="excel_import", defaults={"label": "Importar Excel", "is_enabled": False}
        )
        resp = self.client.post(
            self.URL, {"file": _xlsx_upload()}, format="multipart", **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE, resp.data)


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

    def _spend_with_card(self):
        """Transacción con tarjeta+programa activos, para que la señal de
        `apps.loyalty` le genere un `LoyaltyEarning` (ver
        `apps.loyalty.tests.test_loyalty.LoyaltyEarningSignalTests`)."""
        bank = Bank.objects.create(name="Banco X")
        product = CardProduct.objects.create(bank=bank, name="Signature")
        category_type = CategoryType.objects.create(slug="super", name="Super")
        LoyaltyProgram.objects.create(
            card_product=product, kind=LoyaltyProgram.KIND_CASHBACK, default_rate=Decimal("0.01"),
        )
        card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), card_product=product,
        )
        category = Category.objects.create(
            workspace=self.ws, name="Super", type=Category.TYPE_EXPENSE, category_type=category_type,
        )
        resp = self.client.post(
            "/api/v1/transactions/",
            {"wallet": str(card.id), "category": str(category.id), "amount": "100.00", "date": "2026-09-01"},
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return resp.data["id"]

    def test_free_transaction_hides_loyalty_earnings(self):
        # El cashback igual se genera y se guarda (por si más tarde se pasa a
        # Pro) -- lo que no debe verse en Free es la respuesta de la API.
        txn_id = self._spend_with_card()
        resp = self.client.get(f"/api/v1/transactions/{txn_id}/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["loyalty_earnings"], [])

    def test_pro_transaction_shows_loyalty_earnings(self):
        self.upgrade_to_pro()
        txn_id = self._spend_with_card()
        resp = self.client.get(f"/api/v1/transactions/{txn_id}/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(len(resp.data["loyalty_earnings"]), 1)
        self.assertEqual(resp.data["loyalty_earnings"][0]["kind"], LoyaltyProgram.KIND_CASHBACK)


# ---------------------------------------------------------------------------
# Restricción del gratis del 22-sep-2026 -- ver ECONOMIA-POR-PLAN.md y
# seed_billing_plans.py. Cada gate de acá abajo es nuevo (antes, ninguna de
# estas acciones chequeaba el plan).
# ---------------------------------------------------------------------------
class WalletSplitGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Multimoney")

    def test_free_cannot_split_a_wallet(self):
        resp = self.client.post(
            f"/api/v1/wallets/{self.wallet.id}/split/", {"name": "Ahorro"}, **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        self.assertFalse(Wallet.objects.filter(parent=self.wallet).exists())

    def test_pro_can_split_a_wallet(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            f"/api/v1/wallets/{self.wallet.id}/split/", {"name": "Ahorro"}, **self.headers
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class StatementsGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("1000"), billing_cycle_day=15,
        )

    def test_free_cannot_see_a_single_statement(self):
        resp = self.client.get(f"/api/v1/wallets/{self.card.id}/statement/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_see_the_statements_list(self):
        resp = self.client.get("/api/v1/wallets/statements/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_see_a_statement(self):
        self.upgrade_to_pro()
        resp = self.client.get(f"/api/v1/wallets/{self.card.id}/statement/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class RefundGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        from apps.transactions.models import Category, Transaction

        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta")
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-05-01", type=Transaction.TYPE_EXPENSE,
        )

    def test_free_cannot_register_a_refund(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/register-refund/",
            {"amount": "50.00", "date": "2026-05-10"}, **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.is_refunded)

    def test_pro_can_register_a_refund(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/register-refund/",
            {"amount": "50.00", "date": "2026-05-10"}, **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class SplitCategoriesGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        from apps.transactions.models import Category, Transaction

        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta")
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.hygiene = Category.objects.create(
            workspace=self.ws, name="Higiene", type=Category.TYPE_EXPENSE
        )
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-05-01", type=Transaction.TYPE_EXPENSE,
        )

    def test_free_cannot_split_by_category(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split/",
            {"parts": [
                {"category": str(self.food.id), "amount": "30.00"},
                {"category": str(self.hygiene.id), "amount": "20.00"},
            ]},
            format="json", **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_split_by_category(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split/",
            {"parts": [
                {"category": str(self.food.id), "amount": "30.00"},
                {"category": str(self.hygiene.id), "amount": "20.00"},
            ]},
            format="json", **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class SplitPeopleGateTests(_WorkspaceGateTestCase):
    """`split_people` cubre la persona (`PersonViewSet`), la acción
    `split-people`, `settle-share`, `balances` y `settle-balance` -- todas
    partes de la misma función (dividir gastos con gente)."""

    def setUp(self):
        super().setUp()
        from apps.transactions.models import Category, Person, Transaction

        self.wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta")
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-05-01", type=Transaction.TYPE_EXPENSE,
        )
        self.friend = None  # se crea con Pro en cada test que lo necesita
        self._Person = Person

    def test_free_cannot_create_a_person(self):
        resp = self.client.post("/api/v1/people/", {"name": "Ana"}, **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_split_between_people(self):
        from apps.billing.models import Subscription

        # `friend` se crea con Pro activo (crearla también está detrás de este
        # mismo gate, ver `test_free_cannot_create_a_person`) y luego se
        # vuelve a gratis, para aislar el gate de la acción `split-people` en
        # sí misma, no el de crear la persona.
        self.upgrade_to_pro()
        friend = self._Person.objects.create(workspace=self.ws, name="Ana")
        Subscription.objects.filter(user=self.owner).update(status=Subscription.STATUS_CANCELED)
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(friend.id), "amount": "20.00"}]},
            format="json", **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_cannot_see_balances(self):
        resp = self.client.get("/api/v1/transactions/balances/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_create_a_person_and_split_between_people(self):
        self.upgrade_to_pro()
        friend = self._Person.objects.create(workspace=self.ws, name="Ana")
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(friend.id), "amount": "20.00"}]},
            format="json", **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        resp = self.client.get("/api/v1/transactions/balances/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class InstallmentsGateTests(_WorkspaceGateTestCase):
    def setUp(self):
        super().setUp()
        from apps.transactions.models import Category

        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("1000"), billing_cycle_day=15,
        )
        self.category = Category.objects.create(
            workspace=self.ws, name="Tecnología", type=Category.TYPE_EXPENSE
        )

    def test_free_cannot_create_an_installment_purchase(self):
        resp = self.client.post(
            "/api/v1/installment-purchases/",
            {
                "wallet": str(self.card.id), "category": str(self.category.id),
                "description": "Laptop", "total_amount": "1200.00",
                "installments_total": 12, "start_date": "2026-05-01",
            },
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_pro_can_create_an_installment_purchase(self):
        self.upgrade_to_pro()
        resp = self.client.post(
            "/api/v1/installment-purchases/",
            {
                "wallet": str(self.card.id), "category": str(self.category.id),
                "description": "Laptop", "total_amount": "1200.00",
                "installments_total": 12, "start_date": "2026-05-01",
            },
            **self.headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


class NetWorthGateTests(_WorkspaceGateTestCase):
    def test_free_cannot_see_net_worth_breakdown(self):
        resp = self.client.get("/api/v1/reports/net-worth/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.data)

    def test_free_dashboard_summary_hides_net_worth_but_still_works(self):
        resp = self.client.get("/api/v1/reports/summary/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertIsNone(resp.data["net_worth"])

    def test_pro_can_see_net_worth_breakdown_and_dashboard_figure(self):
        self.upgrade_to_pro()
        resp = self.client.get("/api/v1/reports/net-worth/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        resp = self.client.get("/api/v1/reports/summary/", **self.headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertIsNotNone(resp.data["net_worth"])
