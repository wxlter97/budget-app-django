"""Detección de posibles duplicados: bloqueo en importación (Apple Pay vía
quickadd, correo vía email_import) y aviso no bloqueante al cargar a mano."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.email_import.models import BankEmailSchema, EmailImportLog
from apps.quickadd.models import PersonalAccessToken
from apps.transactions.models import Category, Transaction
from apps.transactions.services import find_possible_duplicates
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class FindPossibleDuplicatesTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="Casa")
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def test_finds_same_day_same_amount(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        matches = find_possible_duplicates(
            wallet=self.wallet, amount=Decimal("25.00"), date=dt.date(2026, 3, 10)
        )
        self.assertEqual(matches.count(), 1)

    def test_finds_within_one_day_window(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        matches = find_possible_duplicates(
            wallet=self.wallet, amount=Decimal("25.00"), date=dt.date(2026, 3, 11)
        )
        self.assertEqual(matches.count(), 1)

    def test_does_not_match_beyond_window(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        matches = find_possible_duplicates(
            wallet=self.wallet, amount=Decimal("25.00"), date=dt.date(2026, 3, 12)
        )
        self.assertEqual(matches.count(), 0)

    def test_does_not_match_different_amount_or_wallet(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        self.assertEqual(
            find_possible_duplicates(
                wallet=self.wallet, amount=Decimal("25.01"), date=dt.date(2026, 3, 10)
            ).count(),
            0,
        )
        other_wallet = Wallet.objects.create(workspace=self.ws, name="Otra", purpose=Wallet.PURPOSE_SPENDING)
        self.assertEqual(
            find_possible_duplicates(
                wallet=other_wallet, amount=Decimal("25.00"), date=dt.date(2026, 3, 10)
            ).count(),
            0,
        )

    def test_exclude_id_skips_the_given_transaction(self):
        txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        matches = find_possible_duplicates(
            wallet=self.wallet, amount=Decimal("25.00"), date=dt.date(2026, 3, 10),
            exclude_id=txn.id,
        )
        self.assertEqual(matches.count(), 0)


class CheckDuplicateEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_manual_entry_check_does_not_block_only_informs(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("25.00"),
            date=dt.date(2026, 3, 10), type=Transaction.TYPE_EXPENSE,
        )
        resp = self.client.get(
            "/api/v1/transactions/check-duplicate/",
            {"wallet": str(self.wallet.id), "amount": "25.00", "date": "2026-03-10"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

        # Y de todos modos se puede crear la nueva -- el endpoint sólo avisa.
        create_resp = self.client.post(
            "/api/v1/transactions/",
            {
                "wallet": str(self.wallet.id), "category": str(self.food.id),
                "amount": "25.00", "date": "2026-03-10", "type": Transaction.TYPE_EXPENSE,
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Transaction.objects.count(), 2)

    def test_no_match_returns_empty_list(self):
        resp = self.client.get(
            "/api/v1/transactions/check-duplicate/",
            {"wallet": str(self.wallet.id), "amount": "999.00", "date": "2026-03-10"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])


class QuickAddDuplicateBlockingTests(APITestCase):
    QUICK_ADD_URL = "/api/v1/quick-add/"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food_group = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        # Sólo las subcategorías (con parent) son asignables -- ver
        # `apps.quickadd.api._resolve_category`.
        cls.restaurants = Category.objects.create(
            workspace=cls.ws, name="Restaurantes", type=Category.TYPE_EXPENSE, parent=cls.food_group,
        )

    def setUp(self):
        _, self.raw_token = PersonalAccessToken.issue(
            user=self.user, workspace=self.ws, wallet=self.wallet, name="Atajo Apple Pay",
        )

    def _post(self, payload):
        return self.client.post(
            self.QUICK_ADD_URL,
            payload,
            HTTP_AUTHORIZATION=f"Bearer {self.raw_token}",
        )

    def test_second_identical_quickadd_same_day_is_blocked(self):
        first = self._post({"amount": "12.50", "merchant": "Starbucks", "category": str(self.restaurants.id)})
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)

        second = self._post({"amount": "12.50", "merchant": "Starbucks", "category": str(self.restaurants.id)})
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT, second.data)
        self.assertEqual(Transaction.objects.filter(source=Transaction.SOURCE_QUICK_ADD).count(), 1)

    def test_different_amount_is_not_blocked(self):
        self._post({"amount": "12.50", "merchant": "Starbucks", "category": str(self.restaurants.id)})
        second = self._post({"amount": "8.00", "merchant": "Starbucks", "category": str(self.restaurants.id)})
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)


class EmailImportDuplicateBlockingTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.schema = BankEmailSchema.objects.create(bank_name="Banco X", sender_pattern="bancox.com")

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def _make_log(self):
        return EmailImportLog.objects.create(
            workspace=self.ws, bank_schema=self.schema, wallet=self.wallet,
            extracted_amount=Decimal("40.00"), extracted_merchant="Super",
            extracted_date=dt.date(2026, 4, 1),
        )

    def test_confirm_blocked_when_transaction_already_exists(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("40.00"),
            date=dt.date(2026, 4, 1), type=Transaction.TYPE_EXPENSE,
            source=Transaction.SOURCE_MANUAL,
        )
        log = self._make_log()
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.food.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.data)
        log.refresh_from_db()
        self.assertEqual(log.status, EmailImportLog.STATUS_PENDING)

    def test_confirm_succeeds_when_no_duplicate(self):
        log = self._make_log()
        resp = self.client.post(
            f"/api/v1/email-import-logs/{log.id}/confirm/",
            {"category": str(self.food.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        log.refresh_from_db()
        self.assertEqual(log.status, EmailImportLog.STATUS_CONFIRMED)
