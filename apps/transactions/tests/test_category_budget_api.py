"""/api/v1/category-budgets/ — CRUD y filtro por período."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.transactions.models import Category, CategoryBudget
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class CategoryBudgetApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")  # budget_period default: monthly
        Membership.objects.create(
            workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER
        )
        self.group = Category.objects.create(
            workspace=self.ws, name="Comida (grupo)", type=Category.TYPE_EXPENSE
        )
        # Los presupuestos sólo se pueden fijar en subcategorías, nunca en el
        # grupo -- ver `CategoryBudgetSerializer.validate_category`.
        self.cat = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE, parent=self.group
        )
        self.client.force_authenticate(self.user)

    def _post(self, **data):
        return self.client.post(
            "/api/v1/category-budgets/", data, **{HEADER: str(self.ws.id)}
        )

    def test_create_and_list(self):
        res = self._post(
            category=str(self.cat.id), amount="300.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CategoryBudget.objects.count(), 1)

    def test_list_filters_by_period_start(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("100"),
            period_start=date(2026, 8, 1),
        )
        sept = CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"),
            period_start=date(2026, 9, 1),
        )
        res = self.client.get(
            "/api/v1/category-budgets/?period_start=2026-09-01", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in res.data["results"]]
        self.assertEqual(ids, [str(sept.id)])

    def test_patch_amount(self):
        b = CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"),
            period_start=date(2026, 9, 1),
        )
        res = self.client.patch(
            f"/api/v1/category-budgets/{b.id}/",
            {"amount": "250.00"},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        b.refresh_from_db()
        self.assertEqual(b.amount, Decimal("250.00"))

    def test_group_cannot_have_its_own_budget(self):
        res = self._post(
            category=str(self.group.id), amount="300.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(CategoryBudget.objects.count(), 0)

    def test_childless_group_can_still_be_budgeted_directly(self):
        # Un grupo sin subcategorías es su propia unidad presupuestable --
        # no hay "hijas" con las que pudiera duplicarse el total.
        leaf_group = Category.objects.create(
            workspace=self.ws, name="Otros", type=Category.TYPE_EXPENSE
        )
        res = self._post(
            category=str(leaf_group.id), amount="150.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_duplicate_same_period_is_rejected(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"),
            period_start=date(2026, 9, 1),
        )
        res = self._post(
            category=str(self.cat.id), amount="99.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_period_start_is_snapped_to_period_boundary(self):
        # `workspace.budget_period` es "monthly": cualquier fecha del mes se
        # ajusta al 1° server-side (ver `CategoryBudgetSerializer.
        # validate_period_start`) -- no hace falta que el cliente calcule el
        # inicio exacto.
        res = self._post(
            category=str(self.cat.id), amount="300.00", period_start="2026-09-17"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["period_start"], "2026-09-01")

    def _set_forward(self, **data):
        return self.client.post(
            "/api/v1/category-budgets/set-forward/", data, **{HEADER: str(self.ws.id)}
        )

    def test_set_forward_creates_current_and_future_periods(self):
        res = self._set_forward(
            category=str(self.cat.id), amount="300.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["periods_touched"], 37)  # mes actual + 36 de horizonte
        dec = CategoryBudget.objects.get(category=self.cat, period_start=date(2026, 12, 1))
        self.assertEqual(dec.amount, Decimal("300.00"))
        far = CategoryBudget.objects.get(category=self.cat, period_start=date(2029, 9, 1))
        self.assertEqual(far.amount, Decimal("300.00"))

    def test_set_forward_stops_at_customized_future_period(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"),
            period_start=date(2026, 9, 1),
        )
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"),
            period_start=date(2026, 10, 1),
        )
        # Noviembre ya fue personalizado por el usuario con otro monto.
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("500"),
            period_start=date(2026, 11, 1),
        )

        res = self._set_forward(
            category=str(self.cat.id), amount="350.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["periods_touched"], 2)  # septiembre + octubre

        sep = CategoryBudget.objects.get(category=self.cat, period_start=date(2026, 9, 1))
        oct_ = CategoryBudget.objects.get(category=self.cat, period_start=date(2026, 10, 1))
        nov = CategoryBudget.objects.get(category=self.cat, period_start=date(2026, 11, 1))
        self.assertEqual(sep.amount, Decimal("350.00"))
        self.assertEqual(oct_.amount, Decimal("350.00"))
        self.assertEqual(nov.amount, Decimal("500"))  # intacto

    def test_set_forward_rejects_group(self):
        res = self._set_forward(
            category=str(self.group.id), amount="300.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(CategoryBudget.objects.count(), 0)

    def test_set_forward_never_touches_past_periods(self):
        past = CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("100"),
            period_start=date(2026, 1, 1),
        )
        res = self._set_forward(
            category=str(self.cat.id), amount="400.00", period_start="2026-09-01"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        past.refresh_from_db()
        self.assertEqual(past.amount, Decimal("100"))
