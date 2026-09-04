"""/api/v1/category-budgets/ — CRUD y filtro por mes/año."""
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
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(
            workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        self.client.force_authenticate(self.user)

    def _post(self, **data):
        return self.client.post(
            "/api/v1/category-budgets/", data, **{HEADER: str(self.ws.id)}
        )

    def test_create_and_list(self):
        res = self._post(
            category=str(self.cat.id), amount="300.00", month=9, year=2026
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CategoryBudget.objects.count(), 1)

    def test_list_filters_by_month_and_year(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("100"), month=8, year=2026
        )
        sept = CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"), month=9, year=2026
        )
        res = self.client.get(
            "/api/v1/category-budgets/?year=2026&month=9", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [row["id"] for row in res.data["results"]]
        self.assertEqual(ids, [str(sept.id)])

    def test_patch_amount(self):
        b = CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"), month=9, year=2026
        )
        res = self.client.patch(
            f"/api/v1/category-budgets/{b.id}/",
            {"amount": "250.00"},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        b.refresh_from_db()
        self.assertEqual(b.amount, Decimal("250.00"))

    def test_duplicate_same_month_is_rejected(self):
        CategoryBudget.objects.create(
            workspace=self.ws, category=self.cat, amount=Decimal("300"), month=9, year=2026
        )
        res = self._post(
            category=str(self.cat.id), amount="99.00", month=9, year=2026
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
