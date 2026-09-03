"""Endpoint /reports/scheduled/ — recurrentes + cuotas próximas sin crearlas."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import (
    Category,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class ScheduledEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.cat = Category.objects.create(
            workspace=cls.ws, name="Suscripción", type=Category.TYPE_EXPENSE
        )
        cls.rec = RecurringExpense.objects.create(
            workspace=cls.ws, category=cls.cat, wallet=cls.wallet,
            amount=Decimal("4.00"), frequency=RecurringExpense.FREQUENCY_MONTHLY,
            next_due_date=dt.date(2026, 3, 1),
        )
        cls.inst = InstallmentPurchase.objects.create(
            workspace=cls.ws, wallet=cls.wallet, category=cls.cat,
            description="Tele", total_amount=Decimal("120.00"),
            installment_amount=Decimal("10.00"), installments_total=12,
            installments_paid=1, start_date=dt.date(2026, 2, 10),
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def test_lists_upcoming_without_creating(self):
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        kinds = sorted(i["kind"] for i in res.data)
        self.assertEqual(kinds, ["installment", "recurring"])
        # no se creó ninguna transacción
        self.assertEqual(Transaction.objects.count(), 0)
        # la recurrente no avanzó
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_due_date, dt.date(2026, 3, 1))

    def test_window_bounds(self):
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-04-01&until=2026-04-30",
            **{HEADER: str(self.ws.id)},
        )
        dates = [i["date"] for i in res.data]
        self.assertTrue(all(d.startswith("2026-04") for d in dates))

    def test_bad_date(self):
        res = self.client.get(
            "/api/v1/reports/scheduled/?until=nope", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
