"""
Aislamiento multi-tenant del API v1.

Dado el esquema compartido (todos los workspaces viven en las mismas tablas),
esto es la garantía crítica: un usuario del workspace A no puede LEER ni
ESCRIBIR datos del workspace B, ni siquiera conociendo los UUID.

El workspace activo se selecciona con el header ``X-Workspace-ID``.
"""
import datetime as dt
import uuid

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Account
from apps.transactions.models import Category, CategoryBudget, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

HEADER = "HTTP_X_WORKSPACE_ID"


def make_workspace(owner, name):
    ws = Workspace.objects.create(name=name)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.ROLE_OWNER)
    return ws


def seed_financials(workspace, owner):
    """Crea una cuenta compartida, categoría, transacción y presupuesto."""
    account = Account.objects.create(
        workspace=workspace, name=f"Cuenta {workspace.name}", type=Account.TYPE_CHECKING
    )
    category = Category.objects.create(
        workspace=workspace, name=f"Cat {workspace.name}", type=Category.TYPE_EXPENSE
    )
    txn = Transaction.objects.create(
        account=account,
        category=category,
        amount="42.00",
        date=dt.date(2026, 1, 15),
        created_by=owner,
    )
    budget = CategoryBudget.objects.create(
        workspace=workspace, category=category, amount="500.00", month=1, year=2026
    )
    return {"account": account, "category": category, "txn": txn, "budget": budget}


class WorkspaceIsolationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.bob = User.objects.create_user("bob", "bob@example.com", "pw")

        cls.ws_a = make_workspace(cls.alice, "A")
        cls.ws_b = make_workspace(cls.bob, "B")

        cls.data_a = seed_financials(cls.ws_a, cls.alice)
        cls.data_b = seed_financials(cls.ws_b, cls.bob)

    def setUp(self):
        # Todas las peticiones se hacen como Alice (miembro de A, ajena a B).
        self.client.force_authenticate(self.alice)

    def as_a(self):
        self.client.credentials(**{HEADER: str(self.ws_a.id)})

    def as_b(self):
        # Alice apunta al workspace de Bob (no debería poder).
        self.client.credentials(**{HEADER: str(self.ws_b.id)})

    # ------------------------------------------------------------------
    # Header de workspace
    # ------------------------------------------------------------------
    def test_missing_header_is_rejected(self):
        self.client.credentials()  # sin header
        resp = self.client.get("/api/v1/accounts/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_header_is_rejected(self):
        self.client.credentials(**{HEADER: "no-soy-un-uuid"})
        resp = self.client.get("/api/v1/accounts/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_workspace_uuid_is_forbidden(self):
        self.client.credentials(**{HEADER: str(uuid.uuid4())})
        resp = self.client.get("/api/v1/accounts/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_member_workspace_is_forbidden_not_404(self):
        # Alice conoce el UUID de B pero no es miembro -> 403 (no 404: no se
        # filtra la existencia del workspace).
        self.as_b()
        resp = self.client.get("/api/v1/accounts/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------
    # LECTURA: los datos de B nunca aparecen bajo el workspace A
    # ------------------------------------------------------------------
    def test_list_endpoints_only_return_own_workspace(self):
        self.as_a()
        cases = {
            "/api/v1/accounts/": self.data_b["account"].id,
            "/api/v1/categories/": self.data_b["category"].id,
            "/api/v1/transactions/": self.data_b["txn"].id,
            "/api/v1/category-budgets/": self.data_b["budget"].id,
        }
        for url, foreign_id in cases.items():
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, status.HTTP_200_OK)
                returned_ids = {row["id"] for row in resp.data["results"]}
                self.assertNotIn(str(foreign_id), returned_ids)
                self.assertTrue(returned_ids)  # sí ve los suyos

    def test_retrieve_foreign_object_is_404(self):
        self.as_a()
        cases = {
            "accounts": self.data_b["account"].id,
            "categories": self.data_b["category"].id,
            "transactions": self.data_b["txn"].id,
            "category-budgets": self.data_b["budget"].id,
        }
        for resource, foreign_id in cases.items():
            with self.subTest(resource=resource):
                resp = self.client.get(f"/api/v1/{resource}/{foreign_id}/")
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_memberships_of_foreign_workspace_not_visible(self):
        self.as_a()
        resp = self.client.get("/api/v1/memberships/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        user_ids = {row["user"] for row in resp.data["results"]}
        self.assertEqual(user_ids, {self.alice.id})

    def test_foreign_workspace_not_in_workspace_list(self):
        # /workspaces/ no usa header: se basa en las Membership de Alice.
        self.client.credentials()
        resp = self.client.get("/api/v1/workspaces/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in resp.data["results"]}
        self.assertIn(str(self.ws_a.id), ids)
        self.assertNotIn(str(self.ws_b.id), ids)

    # ------------------------------------------------------------------
    # ESCRITURA: no se puede tocar nada de B
    # ------------------------------------------------------------------
    def test_cannot_update_foreign_object(self):
        self.as_a()
        resp = self.client.patch(
            f"/api/v1/accounts/{self.data_b['account'].id}/", {"name": "hackeada"}
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.data_b["account"].refresh_from_db()
        self.assertNotEqual(self.data_b["account"].name, "hackeada")

    def test_cannot_delete_foreign_object(self):
        self.as_a()
        resp = self.client.delete(f"/api/v1/transactions/{self.data_b['txn'].id}/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Transaction.objects.filter(pk=self.data_b["txn"].id).exists())

    def test_cannot_create_in_foreign_workspace_via_header(self):
        self.as_b()  # Alice apunta a B
        resp = self.client.post(
            "/api/v1/categories/", {"name": "intrusa", "type": Category.TYPE_EXPENSE}
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Category.objects.filter(name="intrusa").exists())

    def test_cannot_reference_foreign_account_when_creating_transaction(self):
        self.as_a()  # header válido (workspace A) ...
        resp = self.client.post(
            "/api/v1/transactions/",
            {
                "account": str(self.data_b["account"].id),  # ... pero cuenta de B
                "category": str(self.data_a["category"].id),
                "amount": "10.00",
                "date": "2026-02-01",
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("account", resp.data)

    def test_cannot_reference_foreign_category_in_budget(self):
        self.as_a()
        resp = self.client.post(
            "/api/v1/category-budgets/",
            {
                "category": str(self.data_b["category"].id),
                "amount": "100.00",
                "month": 3,
                "year": 2026,
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_owner_cannot_manage_memberships(self):
        # Alice invita a Bob a SU workspace -> ok (es owner de A).
        self.as_a()
        ok = self.client.post("/api/v1/memberships/", {"email": "bob@example.com", "role": "member"})
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.data)

        # Ahora Bob (member de A) intenta invitar a alguien -> prohibido.
        self.client.force_authenticate(self.bob)
        self.client.credentials(**{HEADER: str(self.ws_a.id)})
        forbidden = self.client.post(
            "/api/v1/memberships/", {"email": "alice@example.com", "role": "member"}
        )
        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------
    # Sanidad: el camino feliz de Alice en su propio workspace funciona
    # ------------------------------------------------------------------
    def test_happy_path_in_own_workspace(self):
        self.as_a()
        resp = self.client.post(
            "/api/v1/accounts/",
            {"name": "Nueva", "type": Account.TYPE_CASH},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        created = Account.objects.get(pk=resp.data["id"])
        self.assertEqual(created.workspace_id, self.ws_a.id)
