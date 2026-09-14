"""Endpoint /reports/scheduled/ — recurrentes + cuotas próximas sin crearlas."""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

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

# "Hoy" congelado para estos tests: la cuota 1 (corte 15-feb) ya venció: la
# 2 (corte 15-mar) y las siguientes, todavía no -- son las "próximas".
TODAY = dt.date(2026, 3, 10)


@patch("django.utils.timezone.localdate", return_value=TODAY)
class ScheduledEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Tarjeta", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, billing_cycle_day=15,
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
            installments_total=12, start_date=dt.date(2026, 2, 10),
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def test_lists_upcoming_without_creating(self, _localdate):
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        kinds = sorted(i["kind"] for i in res.data)
        self.assertEqual(kinds, ["installment", "recurring"])
        installment_item = next(i for i in res.data if i["kind"] == "installment")
        self.assertEqual(installment_item["date"], "2026-03-15")
        self.assertIn("cuota 2/12", installment_item["description"])
        # no se creó ninguna transacción
        self.assertEqual(Transaction.objects.count(), 0)
        # la recurrente no avanzó
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.next_due_date, dt.date(2026, 3, 1))

    def test_window_bounds(self, _localdate):
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-04-01&until=2026-04-30",
            **{HEADER: str(self.ws.id)},
        )
        dates = [i["date"] for i in res.data]
        self.assertTrue(all(d.startswith("2026-04") for d in dates))

    def test_already_due_installments_are_not_listed_as_upcoming(self, _localdate):
        # La cuota 1 (corte 15-feb) ya pasó respecto a TODAY -- no debe
        # aparecer aunque el rango consultado la incluya.
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-02-01&until=2026-02-28",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual([i for i in res.data if i["kind"] == "installment"], [])

    def test_bad_date(self, _localdate):
        res = self.client.get(
            "/api/v1/reports/scheduled/?until=nope", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_card_payment_due_date_is_listed(self, _localdate):
        # corte el 5 (ya pasó este mes respecto a TODAY=10 de marzo), pago el
        # 25 -- cae en el mismo mes del corte (25 > 5), después de TODAY.
        card = Wallet.objects.create(
            workspace=self.ws, name="Visa", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, billing_cycle_day=5, payment_due_day=25,
        )
        Transaction.objects.create(
            wallet=card, category=self.cat, amount=Decimal("50.00"),
            date=dt.date(2026, 3, 6), type=Transaction.TYPE_EXPENSE,
        )
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        item = next(i for i in res.data if i["kind"] == "card_payment")
        self.assertEqual(item["date"], "2026-03-25")
        self.assertEqual(item["amount"], "50.00")
        self.assertIn("Visa", item["description"])

    def test_card_without_payment_due_day_has_no_card_payment_item(self, _localdate):
        # `cls.wallet` tiene billing_cycle_day pero no payment_due_day --
        # `credit_card_statement` no puede calcular una fecha de pago.
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-01-01&until=2026-12-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual([i for i in res.data if i["kind"] == "card_payment"], [])

    def test_debt_due_date_is_listed(self, _localdate):
        debt = Wallet.objects.create(
            workspace=self.ws, name="Préstamo carro", purpose=Wallet.PURPOSE_DEBT,
            opening_balance=Decimal("-800.00"), due_date=dt.date(2026, 3, 20),
        )
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        item = next(i for i in res.data if i["kind"] == "debt_due")
        self.assertEqual(item["date"], "2026-03-20")
        self.assertEqual(item["amount"], "800.00")
        self.assertIn("Préstamo carro", item["description"])
        self.assertEqual(item["wallet"], str(debt.id))

    def test_debt_due_date_outside_window_is_not_listed(self, _localdate):
        Wallet.objects.create(
            workspace=self.ws, name="Préstamo carro", purpose=Wallet.PURPOSE_DEBT,
            opening_balance=Decimal("-800.00"), due_date=dt.date(2026, 6, 1),
        )
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual([i for i in res.data if i["kind"] == "debt_due"], [])

    def test_transfer_recurring_shows_destination_wallet_and_no_category(self, _localdate):
        savings = Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS
        )
        RecurringExpense.objects.create(
            workspace=self.ws, wallet=self.wallet, to_wallet=savings,
            type=RecurringExpense.TYPE_TRANSFER, amount=Decimal("25.00"),
            next_due_date=dt.date(2026, 3, 1),
        )
        res = self.client.get(
            "/api/v1/reports/scheduled/?since=2026-03-01&until=2026-03-31",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        transfer_item = next(
            i for i in res.data if i["kind"] == "recurring" and i["amount"] == "25.00"
        )
        self.assertIsNone(transfer_item["category"])
        self.assertEqual(transfer_item["to_wallet"], str(savings.id))
        self.assertIn(savings.name, transfer_item["description"])
