"""Etiquetas libres en transacciones (mejora sugerida en la hoja de ruta):
crear al vuelo desde una transacción, filtrar por etiqueta, y ver el total
acumulado de cada una."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Tag, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class TagApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Efectivo")
        cls.food = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)
        cls.salary = Category.objects.create(workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_create_tag(self):
        resp = self.client.post("/api/v1/tags/", {"name": "viaje-cancún"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["name"], "viaje-cancún")

    def test_create_tag_rejects_case_insensitive_duplicate(self):
        Tag.objects.create(workspace=self.ws, name="Viaje")
        resp = self.client.post("/api/v1/tags/", {"name": "viaje"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_tag_strips_whitespace_and_rejects_blank(self):
        resp = self.client.post("/api/v1/tags/", {"name": "  trabajo  "})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["name"], "trabajo")
        resp2 = self.client.post("/api/v1/tags/", {"name": "   "})
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_tag(self):
        tag = Tag.objects.create(workspace=self.ws, name="ocio")
        resp = self.client.delete(f"/api/v1/tags/{tag.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        tag.refresh_from_db()
        self.assertTrue(tag.is_deleted)

    def test_rename_tag(self):
        tag = Tag.objects.create(workspace=self.ws, name="ocio")
        resp = self.client.patch(f"/api/v1/tags/{tag.id}/", {"name": "entretenimiento"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["name"], "entretenimiento")

    def test_isolated_per_workspace(self):
        other_ws = Workspace.objects.create(name="B")
        Membership.objects.create(workspace=other_ws, user=self.user, role=Membership.ROLE_OWNER)
        Tag.objects.create(workspace=other_ws, name="ajena")
        resp = self.client.get("/api/v1/tags/")
        rows = resp.data if isinstance(resp.data, list) else resp.data["results"]
        names = [t["name"] for t in rows]
        self.assertNotIn("ajena", names)


class TransactionTagAssignmentTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Efectivo")
        cls.food = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def _create_txn(self, **overrides):
        payload = {
            "wallet": str(self.wallet.id),
            "category": str(self.food.id),
            "amount": "20.00",
            "date": "2026-08-10",
            "description": "cena",
        }
        payload.update(overrides)
        return self.client.post("/api/v1/transactions/", payload, format="json")

    def test_creates_tags_on_the_fly(self):
        resp = self._create_txn(tag_names=["Viaje-Cancún", "comida"])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        names = sorted(t["name"] for t in resp.data["tags"])
        self.assertEqual(names, ["Viaje-Cancún", "comida"])
        self.assertEqual(Tag.objects.filter(workspace=self.ws).count(), 2)

    def test_reuses_existing_tag_case_insensitively(self):
        Tag.objects.create(workspace=self.ws, name="Viaje")
        resp = self._create_txn(tag_names=["viaje"])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Tag.objects.filter(workspace=self.ws).count(), 1)
        self.assertEqual(resp.data["tags"][0]["name"], "Viaje")

    def test_dedupes_repeated_names_case_insensitively(self):
        resp = self._create_txn(tag_names=["ocio", "Ocio", " ocio "])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(resp.data["tags"]), 1)

    def test_rejects_more_than_eight_tags(self):
        resp = self._create_txn(tag_names=[f"t{i}" for i in range(9)])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_replaces_tags(self):
        create = self._create_txn(tag_names=["a", "b"])
        txn_id = create.data["id"]
        resp = self.client.patch(f"/api/v1/transactions/{txn_id}/", {"tag_names": ["c"]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([t["name"] for t in resp.data["tags"]], ["c"])

    def test_omitting_tag_names_on_update_keeps_existing_tags(self):
        create = self._create_txn(tag_names=["a"])
        txn_id = create.data["id"]
        resp = self.client.patch(f"/api/v1/transactions/{txn_id}/", {"description": "otra cosa"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([t["name"] for t in resp.data["tags"]], ["a"])

    def test_filter_transactions_by_tag(self):
        tagged = self._create_txn(tag_names=["viaje"]).data
        self._create_txn(description="sin etiqueta")
        tag_id = tagged["tags"][0]["id"]
        resp = self.client.get(f"/api/v1/transactions/?tag={tag_id}")
        rows = resp.data if isinstance(resp.data, list) else resp.data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], tagged["id"])

    def test_split_carries_tags_to_each_part(self):
        create = self._create_txn(tag_names=["viaje"], amount="30.00")
        txn_id = create.data["id"]
        resp = self.client.post(
            f"/api/v1/transactions/{txn_id}/split/",
            {"parts": [{"category": str(self.food.id), "amount": "10.00"},
                       {"category": str(self.food.id), "amount": "20.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        for part in resp.data:
            self.assertEqual([t["name"] for t in part["tags"]], ["viaje"])


class TagSummaryTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="A")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(workspace=cls.ws, name="Efectivo")
        cls.food = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)
        cls.salary = Category.objects.create(workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME)
        cls.trip = Tag.objects.create(workspace=cls.ws, name="viaje")
        cls.empty_tag = Tag.objects.create(workspace=cls.ws, name="sin-uso")

        t1 = Transaction.objects.create(
            wallet=cls.wallet, category=cls.food, amount=Decimal("50.00"), date=dt.date(2026, 8, 1)
        )
        t1.tags.set([cls.trip])
        t2 = Transaction.objects.create(
            wallet=cls.wallet, category=cls.food, amount=Decimal("30.00"), date=dt.date(2026, 8, 5)
        )
        t2.tags.set([cls.trip])
        t3 = Transaction.objects.create(
            wallet=cls.wallet, category=cls.salary, amount=Decimal("100.00"), date=dt.date(2026, 8, 3)
        )
        t3.tags.set([cls.trip])

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})

    def test_summary_aggregates_by_tag(self):
        resp = self.client.get("/api/v1/tags/summary/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        by_id = {r["id"]: r for r in resp.data}
        trip = by_id[str(self.trip.id)]
        self.assertEqual(trip["expense"], "80.00")
        self.assertEqual(trip["income"], "100.00")
        self.assertEqual(trip["count"], 3)
        self.assertEqual(trip["first_date"], "2026-08-01")
        self.assertEqual(trip["last_date"], "2026-08-05")

    def test_summary_includes_unused_tags_with_zero_totals(self):
        resp = self.client.get("/api/v1/tags/summary/")
        by_id = {r["id"]: r for r in resp.data}
        unused = by_id[str(self.empty_tag.id)]
        self.assertEqual(unused["expense"], "0.00")
        self.assertEqual(unused["count"], 0)
        self.assertIsNone(unused["first_date"])
