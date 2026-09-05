"""Fase 1 del clon de Buddy: grupos de categorías, transferencias con
categoría que cuentan al presupuesto, reorden/restaurar, recurrencia ampliada."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.reports.services import budget_vs_actual
from apps.transactions.models import Category, CategoryBudget, RecurringExpense, Transaction
from apps.transactions.services import _advance
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class CategoryGroupTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.group = Category.objects.create(
            workspace=cls.ws, name="Vivienda", type=Category.TYPE_EXPENSE
        )
        cls.child = Category.objects.create(
            workspace=cls.ws, name="Internet", type=Category.TYPE_EXPENSE, parent=cls.group
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _h(self):
        return {HEADER: str(self.ws.id)}

    def test_is_group_flag(self):
        res = self.client.get(f"/api/v1/categories/{self.group.id}/", **self._h())
        self.assertTrue(res.data["is_group"])
        res = self.client.get(f"/api/v1/categories/{self.child.id}/", **self._h())
        self.assertFalse(res.data["is_group"])

    def test_parent_must_be_group(self):
        res = self.client.post(
            "/api/v1/categories/",
            {"name": "X", "type": "expense", "parent": str(self.child.id)},
            format="json",
            **self._h(),
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_only_groups(self):
        res = self.client.get("/api/v1/categories/?parent__isnull=true", **self._h())
        names = {c["name"] for c in res.data["results"]}
        self.assertIn("Vivienda", names)
        self.assertNotIn("Internet", names)

    def test_reorder(self):
        a = Category.objects.create(workspace=self.ws, name="A", type="expense")
        b = Category.objects.create(workspace=self.ws, name="B", type="expense")
        res = self.client.post(
            "/api/v1/categories/reorder/",
            {"ids": [str(b.id), str(a.id)]},
            format="json",
            **self._h(),
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        b.refresh_from_db()
        a.refresh_from_db()
        self.assertLess(b.sort_order, a.sort_order)

    def test_deleted_and_restore(self):
        c = Category.objects.create(workspace=self.ws, name="Temp", type="expense")
        c.soft_delete()
        res = self.client.get("/api/v1/categories/deleted/", **self._h())
        self.assertEqual([x["name"] for x in res.data], ["Temp"])
        res = self.client.post(f"/api/v1/categories/{c.id}/restore/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(Category.objects.filter(id=c.id).exists())

    def test_purge_deletes_for_real(self):
        c = Category.objects.create(workspace=self.ws, name="Temp", type="expense")
        c.soft_delete()
        res = self.client.delete(f"/api/v1/categories/{c.id}/purge/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Category.all_objects.filter(id=c.id).exists())

    def test_purge_requires_soft_deleted_first(self):
        c = Category.objects.create(workspace=self.ws, name="Vivo", type="expense")
        res = self.client.delete(f"/api/v1/categories/{c.id}/purge/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Category.objects.filter(id=c.id).exists())

    def test_purge_blocked_by_live_transactions(self):
        wallet = Wallet.objects.create(workspace=self.ws, name="Efectivo", currency="USD")
        c = Category.objects.create(workspace=self.ws, name="Con movimientos", type="expense")
        Transaction.objects.create(
            wallet=wallet, category=c, amount=Decimal("10.00"),
            date=dt.date(2026, 1, 1), type=Transaction.TYPE_EXPENSE,
        )
        c.soft_delete()
        res = self.client.delete(f"/api/v1/categories/{c.id}/purge/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Category.all_objects.filter(id=c.id).exists())

    def test_purge_blocked_by_live_subcategories(self):
        self.group.soft_delete()
        res = self.client.delete(f"/api/v1/categories/{self.group.id}/purge/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Category.all_objects.filter(id=self.group.id).exists())

    def test_report_ignores_legacy_budget_on_group_with_children(self):
        # `self.group` (Vivienda) tiene una hija (`self.child`, Internet).
        # Un presupuesto directo en el grupo -- ya no se puede crear por API,
        # pero pudo quedar de antes -- no debe sumarse aparte del de la hija.
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.group, amount=Decimal("999.00"),
            month=6, year=2026,
        )
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.child, amount=Decimal("100.00"),
            month=6, year=2026,
        )
        report = budget_vs_actual(self.ws, self.user, 2026, 6)
        grp = next(g for g in report["groups"] if g["group_name"] == "Vivienda")
        self.assertEqual(grp["budgeted"], Decimal("100.00"))


class TransferWithCategoryBudgetTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("bob", "b@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.checking = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("2000.00"),
        )
        cls.savings = Wallet.objects.create(
            workspace=cls.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS,
        )
        cls.ahorro_group = Category.objects.create(
            workspace=cls.ws, name="Ahorro", type=Category.TYPE_EXPENSE
        )
        CategoryBudget.objects.create(
            workspace=cls.ws, category=cls.ahorro_group, amount=Decimal("500.00"),
            year=2026, month=3,
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def test_categorized_transfer_counts_toward_budget(self):
        res = self.client.post(
            "/api/v1/transactions/",
            {
                "type": "transfer",
                "wallet": str(self.checking.id),
                "to_wallet": str(self.savings.id),
                "category": str(self.ahorro_group.id),
                "amount": "500.00",
                "date": "2026-03-05",
            },
            format="json",
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(res.data["category"]), str(self.ahorro_group.id))

        report = budget_vs_actual(self.ws, self.user, 2026, 3)
        row = next(r for r in report["rows"] if r["category_name"] == "Ahorro")
        self.assertEqual(row["spent"], Decimal("500.00"))
        self.assertEqual(row["remaining"], Decimal("0.00"))

        # y aparece agrupado
        grp = next(g for g in report["groups"] if g["group_name"] == "Ahorro")
        self.assertEqual(grp["spent"], Decimal("500.00"))

    def test_uncategorized_transfer_ignored_by_budget(self):
        Transaction.objects.create(
            type="transfer", wallet=self.checking, to_wallet=self.savings,
            amount=Decimal("100.00"), date=dt.date(2026, 3, 6),
        )
        report = budget_vs_actual(self.ws, self.user, 2026, 3)
        rows = [r for r in report["rows"] if r["spent"] != Decimal("0")]
        self.assertEqual(rows, [])

    def test_balance_still_moves_on_categorized_transfer(self):
        self.client.post(
            "/api/v1/transactions/",
            {
                "type": "transfer", "wallet": str(self.checking.id),
                "to_wallet": str(self.savings.id), "category": str(self.ahorro_group.id),
                "amount": "500.00", "date": "2026-03-05",
            },
            format="json",
            **{HEADER: str(self.ws.id)},
        )
        self.checking.refresh_from_db()
        self.savings.refresh_from_db()
        self.assertEqual(self.checking.current_balance, Decimal("1500.00"))
        self.assertEqual(self.savings.current_balance, Decimal("500.00"))


class RecurrenceFrequencyTests(APITestCase):
    def test_advance_weekly_and_biweekly(self):
        d = dt.date(2026, 1, 1)
        self.assertEqual(_advance(d, RecurringExpense.FREQUENCY_WEEKLY), dt.date(2026, 1, 8))
        self.assertEqual(_advance(d, RecurringExpense.FREQUENCY_BIWEEKLY), dt.date(2026, 1, 15))
        self.assertEqual(
            _advance(d, RecurringExpense.FREQUENCY_EVERY_3_MONTHS), dt.date(2026, 4, 1)
        )
        self.assertEqual(_advance(d, RecurringExpense.FREQUENCY_YEARLY), dt.date(2027, 1, 1))
