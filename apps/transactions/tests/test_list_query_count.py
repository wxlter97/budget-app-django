"""`GET /transactions/` no puede hacer una consulta por fila.

Cada consulta a Neon cuesta ~25-30 ms de ida y vuelta, así que un N+1 en el
listado se nota enseguida: Sentry lo reportó a 865 ms con unas decenas de
transacciones (issues de /api/v1/transactions/, release budget-api-00074).
El test no fija un número de consultas -- eso lo movería cualquier campo nuevo
del serializer -- sino que el total **no crezca con la cantidad de filas**.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Tag, Transaction
from apps.transactions.services import register_refund
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class TransactionListQueryCountTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.tag = Tag.objects.create(workspace=cls.ws, name="ocio")

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _add_rows(self, n):
        """`n` gastos con etiqueta; la mitad, además, reembolsados."""
        for i in range(n):
            txn = Transaction.objects.create(
                wallet=self.wallet, category=self.food, amount=Decimal("10.00"),
                date="2026-05-01", type=Transaction.TYPE_EXPENSE, description=f"Gasto {i}",
            )
            txn.tags.add(self.tag)
            if i % 2 == 0:
                register_refund(
                    original=txn, amount=Decimal("10.00"), date="2026-05-02",
                    wallet=self.wallet, created_by=self.user,
                )

    def _list_query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/v1/transactions/", **{HEADER: str(self.ws.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx)

    def test_query_count_does_not_grow_with_the_number_of_rows(self):
        self._add_rows(2)
        few = self._list_query_count()

        self._add_rows(8)
        many = self._list_query_count()

        self.assertEqual(
            many, few,
            f"{many} consultas con más filas contra {few} con pocas: hay un N+1 en el listado.",
        )
