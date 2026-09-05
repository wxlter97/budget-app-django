"""Detección de candidatas a recurrente (Fase 2 del roadmap: "esto se repite
hace 3 meses, ¿lo marco como recurrente?")."""
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import (
    Category,
    RecurringExpense,
    RecurringSuggestionDismissal,
    Transaction,
)
from apps.transactions.services import detect_recurring_candidates
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"
_FIRST = timezone.localdate().replace(day=1)


def month_ago(n, day=5):
    return (_FIRST - relativedelta(months=n)).replace(day=day)


class RecurringDetectionServiceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.acc = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.streaming = Category.objects.create(
            workspace=cls.ws, name="Streaming", type=Category.TYPE_EXPENSE
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def _txn(self, cat, amount, months_ago_n, day=5, **kwargs):
        return Transaction.objects.create(
            wallet=self.acc, category=cat, amount=Decimal(amount),
            date=month_ago(months_ago_n, day), **kwargs,
        )

    def test_detects_a_consistent_monthly_charge(self):
        for n, amount in [(3, "15.99"), (2, "15.99"), (1, "16.49"), (0, "15.99")]:
            self._txn(self.streaming, amount, n)

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["category"], self.streaming.id)
        self.assertEqual(c["wallet"], self.acc.id)
        self.assertEqual(c["occurrences"], 4)
        self.assertEqual(c["suggested_amount"], Decimal("16.12"))  # promedio de las 4, redondeado

    def test_ignores_a_month_with_more_than_one_transaction(self):
        for n, amount in [(3, "15.99"), (2, "15.99"), (1, "15.99")]:
            self._txn(self.streaming, amount, n)
        # Dos cargos el mismo mes -> deja de parecer "una suscripción".
        self._txn(self.streaming, "15.99", 0)
        self._txn(self.streaming, "5.00", 0)

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(candidates, [])

    def test_ignores_inconsistent_amounts(self):
        for n, amount in [(3, "10.00"), (2, "60.00"), (1, "10.00"), (0, "60.00")]:
            self._txn(self.food, amount, n)

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(candidates, [])

    def test_requires_minimum_occurrences(self):
        self._txn(self.streaming, "15.99", 1)
        self._txn(self.streaming, "15.99", 0)

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(candidates, [])

    def test_already_recurring_is_excluded(self):
        for n in (3, 2, 1, 0):
            self._txn(self.streaming, "15.99", n)
        RecurringExpense.objects.create(
            workspace=self.ws, category=self.streaming, wallet=self.acc,
            amount=Decimal("15.99"), next_due_date=timezone.localdate(),
        )

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(candidates, [])

    def test_dismissed_suggestion_is_excluded(self):
        for n in (3, 2, 1, 0):
            self._txn(self.streaming, "15.99", n)
        RecurringSuggestionDismissal.objects.create(
            workspace=self.ws, category=self.streaming, wallet=self.acc,
            approx_amount=Decimal("16"),
        )

        candidates = detect_recurring_candidates(self.ws, self.user)
        self.assertEqual(candidates, [])


class RecurringSuggestionApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.acc = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.streaming = Category.objects.create(
            workspace=cls.ws, name="Streaming", type=Category.TYPE_EXPENSE
        )
        for n in (3, 2, 1, 0):
            Transaction.objects.create(
                wallet=cls.acc, category=cls.streaming, amount=Decimal("15.99"),
                date=month_ago(n),
            )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_suggestions_endpoint_shape(self):
        resp = self.client.get("/api/v1/recurring-expenses/suggestions/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        row = resp.data[0]
        self.assertEqual(row["category_name"], "Streaming")
        self.assertEqual(row["wallet_name"], "Cuenta")
        self.assertEqual(row["occurrences"], 4)
        self.assertIn("suggested_next_due_date", row)

    def test_dismiss_then_suggestion_disappears(self):
        resp = self.client.post(
            "/api/v1/recurring-expenses/dismiss-suggestion/",
            {"category": str(self.streaming.id), "wallet": str(self.acc.id), "amount": "15.99"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        resp = self.client.get("/api/v1/recurring-expenses/suggestions/")
        self.assertEqual(resp.data, [])

    def test_accepting_a_suggestion_creates_a_recurring_expense(self):
        row = self.client.get("/api/v1/recurring-expenses/suggestions/").data[0]
        resp = self.client.post(
            "/api/v1/recurring-expenses/",
            {
                "category": row["category"],
                "wallet": row["wallet"],
                "amount": str(row["suggested_amount"]),
                "frequency": "monthly",
                "next_due_date": row["suggested_next_due_date"],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        # Ya está cubierta por un RecurringExpense activo -> deja de sugerirse.
        self.assertEqual(self.client.get("/api/v1/recurring-expenses/suggestions/").data, [])
