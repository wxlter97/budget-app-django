"""
CRUD y aislamiento de los endpoints secundarios (Asset, Liability, Debt,
SavingsGoal, ReserveFund, RecurringExpense, InstallmentPurchase,
MonthlySnapshot). El header X-Workspace-ID manda igual que en el core.
"""
import datetime as dt

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account, Asset, Debt, Liability
from apps.reports.models import MonthlySnapshot
from apps.savings.models import ReserveFund, SavingsGoal
from apps.transactions.models import (
    Category,
    InstallmentPurchase,
    RecurringExpense,
)
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


def make_workspace(owner, name):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


class SecondaryEndpointsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        cls.ws_a = make_workspace(cls.alice, "A")
        cls.ws_b = make_workspace(cls.bob, "B")

        # Un objeto de cada tipo en cada workspace.
        for ws, user in ((cls.ws_a, cls.alice), (cls.ws_b, cls.bob)):
            Asset.objects.create(workspace=ws, name="Casa", type="propiedad", current_value=1)
            Liability.objects.create(
                workspace=ws, name="Hipoteca", type="prestamo", total_amount=10, remaining_amount=9
            )
            Debt.objects.create(
                workspace=ws, direction=Debt.DIRECTION_FAVOR, person="X", amount=5
            )
            SavingsGoal.objects.create(workspace=ws, name="Viaje", target_amount=100)
            ReserveFund.objects.create(workspace=ws, name="Auto", monthly_contribution=10)
            acc = Account.objects.create(workspace=ws, name="C", type=Account.TYPE_CHECKING)
            cat = Category.objects.create(workspace=ws, name="Cat", type=Category.TYPE_EXPENSE)
            RecurringExpense.objects.create(
                workspace=ws, category=cat, account=acc, amount=1,
                next_due_date=dt.date(2026, 2, 1),
            )
            InstallmentPurchase.objects.create(
                workspace=ws, account=acc, category=cat, description="TV",
                total_amount=12, installment_amount=1, installments_total=12,
                start_date=dt.date(2026, 1, 1),
            )
            MonthlySnapshot.objects.create(
                workspace=ws, month=1, year=2026, total_net_worth=0,
                total_income=0, total_expenses=0,
            )

    def setUp(self):
        self.client.force_authenticate(self.alice)
        self.client.credentials(**{HEADER: str(self.ws_a.id)})

    ENDPOINTS = [
        "assets", "liabilities", "debts", "savings-goals", "reserve-funds",
        "recurring-expenses", "installment-purchases", "monthly-snapshots",
    ]

    def test_lists_are_scoped_to_active_workspace(self):
        for ep in self.ENDPOINTS:
            with self.subTest(endpoint=ep):
                resp = self.client.get(f"/api/v1/{ep}/")
                self.assertEqual(resp.status_code, status.HTTP_200_OK)
                self.assertEqual(len(resp.data["results"]), 1)

    def test_foreign_objects_are_404(self):
        model_by_ep = {
            "assets": Asset, "liabilities": Liability, "debts": Debt,
            "savings-goals": SavingsGoal, "reserve-funds": ReserveFund,
            "recurring-expenses": RecurringExpense,
            "installment-purchases": InstallmentPurchase,
            "monthly-snapshots": MonthlySnapshot,
        }
        for ep, model in model_by_ep.items():
            foreign = model.objects.filter(workspace=self.ws_b).first()
            with self.subTest(endpoint=ep):
                resp = self.client.get(f"/api/v1/{ep}/{foreign.id}/")
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_monthly_snapshots_are_read_only(self):
        resp = self.client.post(
            "/api/v1/monthly-snapshots/",
            {"month": 2, "year": 2026, "total_net_worth": 0, "total_income": 0, "total_expenses": 0},
        )
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_create_asset_in_active_workspace(self):
        resp = self.client.post("/api/v1/assets/", {"name": "Auto", "type": "vehiculo", "current_value": "5000.00"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(Asset.objects.get(pk=resp.data["id"]).workspace_id, self.ws_a.id)

    def test_recurring_expense_rejects_foreign_account(self):
        foreign_account = Account.objects.filter(workspace=self.ws_b).first()
        own_category = Category.objects.filter(workspace=self.ws_a).first()
        resp = self.client.post(
            "/api/v1/recurring-expenses/",
            {
                "account": str(foreign_account.id),
                "category": str(own_category.id),
                "amount": "1.00",
                "next_due_date": "2026-03-01",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("account", resp.data)
