"""`GET /wallets/` no puede hacer consultas por cartera.

Sentry lo reportó como N+1 en /api/v1/wallets/: por cada cartera se pedían sus
tarjetas adicionales y, recursivamente, sus hijas (`aggregated_balance`). Con
~25 ms de ida y vuelta a Neon por consulta, se paga en cada apertura de la app.
El test mide que el total **no crezca con la cantidad de carteras**.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet, WalletCard
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class WalletListQueryCountTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _add_wallet_trees(self, n):
        """`n` carteras raíz, cada una con tarjeta adicional, una hija y una nieta."""
        for i in range(n):
            root = Wallet.objects.create(
                workspace=self.ws, name=f"Raíz {i}", purpose=Wallet.PURPOSE_SPENDING
            )
            WalletCard.objects.create(wallet=root, last4="1234", label="Adicional")
            child = Wallet.objects.create(
                workspace=self.ws, name=f"Hija {i}", purpose=Wallet.PURPOSE_SAVINGS, parent=root
            )
            Wallet.objects.create(
                workspace=self.ws, name=f"Nieta {i}", purpose=Wallet.PURPOSE_SAVINGS, parent=child
            )

    def _list_query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/v1/wallets/", **{HEADER: str(self.ws.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx)

    def test_query_count_does_not_grow_with_the_number_of_wallets(self):
        self._add_wallet_trees(1)
        few = self._list_query_count()

        self._add_wallet_trees(5)
        many = self._list_query_count()

        self.assertEqual(
            many, few,
            f"{many} consultas con más carteras contra {few} con pocas: hay un N+1 en el listado.",
        )
