import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.email_import.bank_parsers import ParsedEmail
from apps.email_import.bank_parsers.demo_bank import parse as demo_parse
from apps.email_import.bank_parsers.nu_style import parse as nu_parse
from apps.email_import.models import BankEmailSchema, EmailImportLog
from apps.email_import.services import (
    WorkspaceNotResolved,
    ingest_inbound_email,
    resolve_workspace,
)
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

DEMO_BODY = (
    "Estimado cliente, le informamos: Compra por USD 1,234.56 en STARBUCKS "
    "REFORMA con tarjeta terminada en 4321 el 20/01/2026. Gracias."
)
SECRET = "test-inbound-secret"


class ParserUnitTests(TestCase):
    def test_demo_bank_parser(self):
        parsed = demo_parse("Alerta de compra", DEMO_BODY, "alertas@demobank.com")
        self.assertEqual(parsed.amount, Decimal("1234.56"))
        self.assertEqual(parsed.date, dt.date(2026, 1, 20))
        self.assertEqual(parsed.merchant, "STARBUCKS REFORMA")
        self.assertEqual(parsed.card_last4, "4321")
        self.assertEqual(parsed.currency, "USD")

    def test_demo_bank_parser_raises_on_garbage(self):
        from apps.email_import.bank_parsers import ParseError

        with self.assertRaises(ParseError):
            demo_parse("x", "nada que ver aquí", "x@y.com")

    def test_nu_style_parser(self):
        body = (
            "Realizaste una compra\nValor: $ 89.900,00\nComercio: RAPPI COLOMBIA\n"
            "Tarjeta: ****1234\nFecha: 3 feb 2026"
        )
        parsed = nu_parse("Compra", body, "no-reply@nu.com.co")
        self.assertEqual(parsed.amount, Decimal("89900.00"))
        self.assertEqual(parsed.date, dt.date(2026, 2, 3))
        self.assertEqual(parsed.merchant, "RAPPI COLOMBIA")
        self.assertEqual(parsed.card_last4, "1234")


class IngestServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="A")
        cls.schema = BankEmailSchema.objects.create(
            bank_name="Demo Bank", sender_pattern=r"@demobank\.com"
        )
        cls.account = Wallet.objects.create(
            workspace=cls.ws, name="Tarjeta", purpose=Wallet.PURPOSE_DEBT, card_last4="4321"
        )

    def _to(self):
        return f"import+{self.ws.inbound_token}@inbound.budget.local"

    def test_resolve_workspace_by_token(self):
        self.assertEqual(resolve_workspace(self._to()), self.ws)
        self.assertEqual(
            resolve_workspace(["otro@x.com", self._to()]), self.ws
        )

    def test_resolve_workspace_unknown_token(self):
        with self.assertRaises(WorkspaceNotResolved):
            resolve_workspace("import+nope@inbound.budget.local")

    def test_successful_ingest_creates_pending_log_and_matches_wallet(self):
        log = ingest_inbound_email(
            to=self._to(), sender="alertas@demobank.com",
            subject="Alerta", text=DEMO_BODY,
        )
        self.assertEqual(log.status, EmailImportLog.STATUS_PENDING)
        self.assertEqual(log.extracted_amount, Decimal("1234.56"))
        self.assertEqual(log.extracted_merchant, "STARBUCKS REFORMA")
        self.assertEqual(log.extracted_date, dt.date(2026, 1, 20))
        self.assertEqual(log.wallet, self.account)
        self.assertEqual(log.bank_schema, self.schema)

    def test_unknown_sender_creates_failed_log(self):
        log = ingest_inbound_email(
            to=self._to(), sender="spam@random.com", subject="x", text="y"
        )
        self.assertEqual(log.status, EmailImportLog.STATUS_FAILED)
        self.assertIn("no reconocido", log.error_message)

    def test_no_parser_creates_failed_log(self):
        BankEmailSchema.objects.create(
            bank_name="Banco Sin Parser", sender_pattern=r"@sinparser\.com"
        )
        log = ingest_inbound_email(
            to=self._to(), sender="a@sinparser.com", subject="x", text="y"
        )
        self.assertEqual(log.status, EmailImportLog.STATUS_FAILED)
        self.assertIn("Sin parser", log.error_message)

    def test_parse_failure_creates_failed_log(self):
        log = ingest_inbound_email(
            to=self._to(), sender="alertas@demobank.com",
            subject="x", text="cuerpo que no matchea",
        )
        self.assertEqual(log.status, EmailImportLog.STATUS_FAILED)
        self.assertEqual(EmailImportLog.objects.filter(workspace=self.ws).count(), 1)


@override_settings(INBOUND_WEBHOOK_SECRET=SECRET)
class InboundWebhookTests(APITestCase):
    URL = "/api/v1/email-import/inbound/"

    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="A")
        BankEmailSchema.objects.create(
            bank_name="Demo Bank", sender_pattern=r"@demobank\.com"
        )

    def _to(self):
        return f"import+{self.ws.inbound_token}@inbound.budget.local"

    def test_rejects_without_secret(self):
        resp = self.client.post(self.URL, {"to": self._to(), "from": "a@demobank.com"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_rejects_wrong_secret(self):
        resp = self.client.post(
            self.URL, {"to": self._to(), "from": "a@demobank.com"},
            HTTP_X_INBOUND_SECRET="nope",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_workspace_token_is_404(self):
        resp = self.client.post(
            self.URL,
            {"to": "import+bogus@inbound.budget.local", "from": "a@demobank.com"},
            HTTP_X_INBOUND_SECRET=SECRET,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_valid_webhook_creates_pending_log(self):
        resp = self.client.post(
            self.URL,
            {
                "to": self._to(),
                "from": "alertas@demobank.com",
                "subject": "Alerta de compra",
                "text": DEMO_BODY,
            },
            HTTP_X_INBOUND_SECRET=SECRET,
        )
        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertEqual(resp.data["status"], "pending")
        log = EmailImportLog.objects.get(id=resp.data["log_id"])
        self.assertEqual(log.workspace, self.ws)
        self.assertEqual(log.extracted_amount, Decimal("1234.56"))

    def test_mailgun_field_names_are_accepted(self):
        resp = self.client.post(
            self.URL,
            {
                "recipient": self._to(),
                "sender": "alertas@demobank.com",
                "subject": "x",
                "body-plain": DEMO_BODY,
            },
            HTTP_X_INBOUND_SECRET=SECRET,
        )
        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertEqual(resp.data["status"], "pending")

    def test_end_to_end_ingest_then_confirm(self):
        # 1. entra el correo
        resp = self.client.post(
            self.URL,
            {"to": self._to(), "from": "alertas@demobank.com", "text": DEMO_BODY},
            HTTP_X_INBOUND_SECRET=SECRET,
        )
        log_id = resp.data["log_id"]

        # 2. un miembro confirma el log -> se crea la Transaction
        user = User.objects.create_user("alice", "a@example.com", "pw")
        Membership.objects.create(workspace=self.ws, user=user, role=Membership.ROLE_OWNER)
        account = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta", purpose=Wallet.PURPOSE_DEBT
        )
        category = Category.objects.create(
            workspace=self.ws, name="Café", type=Category.TYPE_EXPENSE
        )
        self.client.force_authenticate(user)
        self.client.credentials(HTTP_X_WORKSPACE_ID=str(self.ws.id))
        confirm = self.client.post(
            f"/api/v1/email-import-logs/{log_id}/confirm/",
            {"wallet": str(account.id), "category": str(category.id)},
        )
        self.assertEqual(confirm.status_code, status.HTTP_200_OK, confirm.data)
        txn = EmailImportLog.objects.get(id=log_id).resulting_transaction
        self.assertEqual(txn.source, Transaction.SOURCE_EMAIL_IMPORT)
        self.assertEqual(txn.amount, Decimal("1234.56"))


class WorkspaceInboundTokenTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.member = User.objects.create_user("bob", "b@example.com", "pw")

    def test_workspace_create_exposes_inbound_address(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/v1/workspaces/", {"name": "Casa"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp.data["inbound_token"])
        self.assertIn(resp.data["inbound_token"], resp.data["inbound_email"])

    def test_rotate_token_owner_only(self):
        self.client.force_authenticate(self.user)
        ws_id = self.client.post("/api/v1/workspaces/", {"name": "Casa"}).data["id"]
        original = Workspace.objects.get(id=ws_id).inbound_token

        Membership.objects.create(
            workspace_id=ws_id, user=self.member, role=Membership.ROLE_MEMBER
        )

        # el member no puede
        self.client.force_authenticate(self.member)
        forbidden = self.client.post(f"/api/v1/workspaces/{ws_id}/rotate-inbound-token/")
        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)

        # el owner sí
        self.client.force_authenticate(self.user)
        ok = self.client.post(f"/api/v1/workspaces/{ws_id}/rotate-inbound-token/")
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        self.assertNotEqual(ok.data["inbound_token"], original)
