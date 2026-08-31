import datetime as dt

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.email_import.models import BankEmailSchema, EmailImportLog
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def make_workspace(owner, name):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class EmailImportConfirmTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        cls.ws_a = make_workspace(cls.alice, "A")
        cls.ws_b = make_workspace(cls.bob, "B")

        cls.schema = BankEmailSchema.objects.create(
            bank_name="Banco X", sender_pattern="@bancox.com"
        )
        cls.account_a = Account.objects.create(
            workspace=cls.ws_a, name="Tarjeta", type=Account.TYPE_CREDIT
        )
        cls.category_a = Category.objects.create(
            workspace=cls.ws_a, name="Super", type=Category.TYPE_EXPENSE
        )
        cls.account_b = Account.objects.create(
            workspace=cls.ws_b, name="Ajena", type=Account.TYPE_CREDIT
        )

    def setUp(self):
        self.client.force_authenticate(self.alice)
        self.client.credentials(**{HEADER: str(self.ws_a.id)})

    def _pending(self, **kwargs):
        defaults = dict(
            workspace=self.ws_a,
            bank_schema=self.schema,
            account=self.account_a,
            status=EmailImportLog.STATUS_PENDING,
            raw_email_subject="Compra aprobada",
            extracted_amount="123.45",
            extracted_merchant="SUPERMERCADO",
            extracted_date=dt.date(2026, 1, 20),
        )
        defaults.update(kwargs)
        return EmailImportLog.objects.create(**defaults)

    def test_confirm_creates_transaction_and_links_it(self):
        log = self._pending()
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.category_a.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        log.refresh_from_db()
        self.assertEqual(log.status, EmailImportLog.STATUS_CONFIRMED)
        txn = log.resulting_transaction
        self.assertIsNotNone(txn)
        self.assertEqual(txn.source, Transaction.SOURCE_EMAIL_IMPORT)
        self.assertEqual(str(txn.amount), "123.45")
        self.assertEqual(txn.account_id, self.account_a.id)
        self.assertEqual(txn.date, dt.date(2026, 1, 20))

    def test_confirm_requires_category(self):
        log = self._pending()
        resp = self.client.post(f"/api/v1/email-import-logs/{log.id}/confirm/", {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("category", resp.data)

    def test_confirm_missing_extracted_amount_needs_override(self):
        log = self._pending(extracted_amount=None)
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.category_a.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("amount", resp.data)

        ok = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.category_a.id), "amount": "50.00"},
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK, ok.data)

    def test_cannot_confirm_twice(self):
        log = self._pending()
        self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.category_a.id)},
        )
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.category_a.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Transaction.objects.count(), 1)

    def test_reject_sets_status(self):
        log = self._pending()
        resp = self.client.post(f"/api/v1/email-import-logs/{log.id}/reject/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        log.refresh_from_db()
        self.assertEqual(log.status, EmailImportLog.STATUS_REJECTED)

    def test_confirm_rejects_foreign_category(self):
        foreign_cat = Category.objects.create(
            workspace=self.ws_b, name="Ajena", type=Category.TYPE_EXPENSE
        )
        log = self._pending()
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(foreign_cat.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_filter(self):
        self._pending()
        self._pending(status=EmailImportLog.STATUS_FAILED, account=None)
        resp = self.client.get("/api/v1/email-import-logs/?status=pending")
        self.assertEqual(len(resp.data["results"]), 1)

    # --- aislamiento ---
    def test_foreign_workspace_log_not_visible(self):
        foreign_log = EmailImportLog.objects.create(
            workspace=self.ws_b, status=EmailImportLog.STATUS_PENDING
        )
        self.assertEqual(
            self.client.get(f"/api/v1/email-import-logs/{foreign_log.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/email-import-logs/{foreign_log.id}/confirm/",
                {"category": str(self.category_a.id)},
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class BankEmailSchemaTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@example.com", "pw")
        self.staff = User.objects.create_user("s", "s@example.com", "pw", is_staff=True)
        BankEmailSchema.objects.create(bank_name="Activo", sender_pattern="a", is_active=True)
        BankEmailSchema.objects.create(bank_name="Inactivo", sender_pattern="i", is_active=False)

    def test_regular_user_sees_only_active(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/v1/bank-email-schemas/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = {row["bank_name"] for row in resp.data["results"]}
        self.assertEqual(names, {"Activo"})

    def test_regular_user_cannot_create(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(
            "/api/v1/bank-email-schemas/", {"bank_name": "Nuevo", "sender_pattern": "n"}
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_create(self):
        self.client.force_authenticate(self.staff)
        resp = self.client.post(
            "/api/v1/bank-email-schemas/", {"bank_name": "Nuevo", "sender_pattern": "n"}
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
