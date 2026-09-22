"""Bug reportado: registrar a mano desde "Programado" una ocurrencia de un
recurrente un día antes de que corra el job automático la duplicaba, porque
el alta manual sólo prellenaba el formulario (ver `openScheduledItem` en el
frontend) y nunca tocaba `next_due_date`. El fix: el cliente manda
`recurring_expense` al crear la transacción, y el serializer avanza la regla
(ver `apps.transactions.services.register_manual_recurring_occurrence`)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, RecurringExpense, Transaction
from apps.transactions.services import generate_recurring_transactions
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class ManualRecurringRegistrationApiTests(APITestCase):
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
        cls.rec = RecurringExpense.objects.create(
            workspace=cls.ws, wallet=cls.acc, category=cls.streaming,
            amount=Decimal("15.99"), next_due_date=dt.date(2026, 2, 1),
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def _register(self, **overrides):
        payload = {
            "type": "expense",
            "wallet": str(self.acc.id),
            "category": str(self.streaming.id),
            "amount": "15.99",
            "date": "2026-02-01",
            "source": "recurring",
            "is_recurring": True,
            "recurring_expense": str(self.rec.id),
        }
        payload.update(overrides)
        return self.client.post("/api/v1/transactions/", payload, format="json")

    def test_registering_a_day_early_advances_the_rule_so_the_job_does_not_duplicate(self):
        resp = self._register()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_due_date, dt.date(2026, 3, 1))

        # El día siguiente corre el job automático: no debe generar otra.
        created = generate_recurring_transactions(as_of=dt.date(2026, 2, 2))
        self.assertEqual(created, [])
        self.assertEqual(Transaction.objects.count(), 1)

    def test_rejects_a_recurring_expense_from_another_workspace(self):
        other_ws = Workspace.objects.create(name="B")
        other_wallet = Wallet.objects.create(
            workspace=other_ws, name="Otra", purpose=Wallet.PURPOSE_SPENDING
        )
        other_category = Category.objects.create(
            workspace=other_ws, name="Otra cat", type=Category.TYPE_EXPENSE
        )
        foreign_rec = RecurringExpense.objects.create(
            workspace=other_ws, wallet=other_wallet, category=other_category,
            amount=Decimal("5.00"), next_due_date=dt.date(2026, 2, 1),
        )

        resp = self._register(recurring_expense=str(foreign_rec.id))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recurring_expense", resp.data)

    def test_editing_a_transaction_does_not_advance_the_rule(self):
        resp = self._register()
        txn_id = resp.data["id"]
        self.rec.refresh_from_db()
        due_after_create = self.rec.next_due_date

        resp = self.client.patch(
            f"/api/v1/transactions/{txn_id}/",
            {"recurring_expense": str(self.rec.id), "amount": "16.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_due_date, due_after_create)
