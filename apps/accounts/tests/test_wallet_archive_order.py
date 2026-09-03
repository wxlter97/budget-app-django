"""Fase 1 clon Buddy: archivar/reordenar carteras, kind/credit_limit,
patrimonio neto incluye archivadas."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.reports.services import net_worth_breakdown
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class WalletArchiveOrderTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@e.com", "pw")
        cls.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.w1 = Wallet.objects.create(
            workspace=cls.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("100.00"),
        )
        cls.w2 = Wallet.objects.create(
            workspace=cls.ws, name="Amex", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_CREDIT, credit_limit=Decimal("1000.00"),
            opening_balance=Decimal("-50.00"),
        )

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _h(self):
        return {HEADER: str(self.ws.id)}

    def test_kind_and_credit_limit_roundtrip(self):
        res = self.client.get(f"/api/v1/wallets/{self.w2.id}/", **self._h())
        self.assertEqual(res.data["kind"], "credit")
        self.assertEqual(res.data["credit_limit"], "1000.00")

    def test_archive_hides_from_list_but_keeps_net_worth(self):
        before = net_worth_breakdown(self.ws)["net"]
        res = self.client.post(f"/api/v1/wallets/{self.w2.id}/archive/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        listed = self.client.get("/api/v1/wallets/", **self._h())
        ids = {w["id"] for w in listed.data["results"]}
        self.assertNotIn(str(self.w2.id), ids)

        listed_all = self.client.get("/api/v1/wallets/?is_archived=true", **self._h())
        self.assertEqual({w["id"] for w in listed_all.data["results"]}, {str(self.w2.id)})

        self.assertEqual(net_worth_breakdown(self.ws)["net"], before)

    def test_unarchive(self):
        self.w2.is_archived = True
        self.w2.save()
        res = self.client.post(f"/api/v1/wallets/{self.w2.id}/unarchive/", **self._h())
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.w2.refresh_from_db()
        self.assertFalse(self.w2.is_archived)

    def test_reorder(self):
        res = self.client.post(
            "/api/v1/wallets/reorder/",
            {"ids": [str(self.w2.id), str(self.w1.id)]},
            format="json",
            **self._h(),
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        listed = self.client.get("/api/v1/wallets/", **self._h())
        self.assertEqual(
            [w["id"] for w in listed.data["results"]],
            [str(self.w2.id), str(self.w1.id)],
        )
