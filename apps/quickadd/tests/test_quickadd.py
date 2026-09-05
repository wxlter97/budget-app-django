from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.quickadd.models import PersonalAccessToken
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()


class QuickAddBaseTestCase(APITestCase):
    QUICK_ADD_URL = "/api/v1/quick-add/"

    def setUp(self):
        self.user = User.objects.create_user("alice", "a@example.com", "pw")
        self.workspace = Workspace.objects.create(name="Casa")
        Membership.objects.create(
            workspace=self.workspace, user=self.user, role=Membership.ROLE_OWNER
        )
        self.wallet = Wallet.objects.create(
            workspace=self.workspace, name="Tarjeta", purpose=Wallet.PURPOSE_SPENDING
        )
        self.groceries = Category.objects.create(
            workspace=self.workspace, name="Comida", type=Category.TYPE_EXPENSE
        )
        # "Comida" es grupo por defecto (sin parent) -- las asignables son
        # las subcategorías, igual que en el resto de la app.
        self.restaurants = Category.objects.create(
            workspace=self.workspace,
            name="Restaurantes",
            type=Category.TYPE_EXPENSE,
            parent=self.groceries,
        )

    def _issue_token(self):
        _, raw = PersonalAccessToken.issue(
            user=self.user, workspace=self.workspace, wallet=self.wallet, name="iPhone"
        )
        return raw

    def _auth(self, raw_token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")


class QuickAddViewTests(QuickAddBaseTestCase):
    def test_rejects_without_token(self):
        resp = self.client.post(self.QUICK_ADD_URL, {"amount": "5.00", "merchant": "x"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_bogus_token(self):
        self._auth("bt_live_nope")
        resp = self.client.post(self.QUICK_ADD_URL, {"amount": "5.00", "merchant": "x"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_explicit_category_id_creates_transaction(self):
        raw = self._issue_token()
        self._auth(raw)
        resp = self.client.post(
            self.QUICK_ADD_URL,
            {"amount": "12.50", "merchant": "Super Selectos", "category": str(self.restaurants.id)},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["category"], "Restaurantes")

        txn = Transaction.objects.get(id=resp.data["transaction_id"])
        self.assertEqual(txn.wallet, self.wallet)
        self.assertEqual(txn.amount, Decimal("12.50"))
        self.assertEqual(txn.description, "Super Selectos")
        self.assertEqual(txn.source, Transaction.SOURCE_QUICK_ADD)
        self.assertEqual(txn.created_by, self.user)

    def test_explicit_category_name_case_insensitive(self):
        raw = self._issue_token()
        self._auth(raw)
        resp = self.client.post(
            self.QUICK_ADD_URL,
            {"amount": "8", "merchant": "x", "category": "restaurantes"},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["category"], "Restaurantes")

    def test_auto_category_learns_from_past_transactions(self):
        Transaction.objects.create(
            wallet=self.wallet, category=self.restaurants, amount=Decimal("9.99"),
            description="Starbucks Reforma", date="2026-01-01", type=Transaction.TYPE_EXPENSE,
        )
        raw = self._issue_token()
        self._auth(raw)
        resp = self.client.post(
            self.QUICK_ADD_URL, {"amount": "6.50", "merchant": "Starbucks"},
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["category"], "Restaurantes")

    def test_auto_category_without_history_asks_to_choose(self):
        raw = self._issue_token()
        self._auth(raw)
        resp = self.client.post(
            self.QUICK_ADD_URL, {"amount": "6.50", "merchant": "Comercio nuevo"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # DRF anida el error bajo "category"; lo importante es que trae opciones.
        self.assertIn("categories", str(resp.data))

    def test_unknown_category_id_falls_back_to_prompt(self):
        raw = self._issue_token()
        self._auth(raw)
        resp = self.client.post(
            self.QUICK_ADD_URL,
            {"amount": "6.50", "merchant": "x", "category": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_last_used_at_updates_on_use(self):
        raw = self._issue_token()
        token = PersonalAccessToken.objects.get()
        self.assertIsNone(token.last_used_at)
        self._auth(raw)
        self.client.post(
            self.QUICK_ADD_URL,
            {"amount": "1", "merchant": "x", "category": str(self.restaurants.id)},
        )
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)


class PersonalAccessTokenViewSetTests(QuickAddBaseTestCase):
    LIST_URL = "/api/v1/personal-tokens/"

    def _headers(self):
        return {"HTTP_X_WORKSPACE_ID": str(self.workspace.id)}

    def test_create_returns_raw_token_once(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(
            self.LIST_URL,
            {"name": "iPhone de Alice", "wallet": str(self.wallet.id)},
            **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(resp.data["token"].startswith("bt_live_"))

        listing = self.client.get(self.LIST_URL, **self._headers())
        self.assertIsNone(listing.data["results"][0]["token"])

    def test_cannot_create_token_for_wallet_in_another_workspace(self):
        other_ws = Workspace.objects.create(name="Otro")
        other_wallet = Wallet.objects.create(
            workspace=other_ws, name="Ajena", purpose=Wallet.PURPOSE_SPENDING
        )
        self.client.force_authenticate(self.user)
        resp = self.client.post(
            self.LIST_URL,
            {"name": "x", "wallet": str(other_wallet.id)},
            **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_only_sees_own_tokens(self):
        bob = User.objects.create_user("bob", "b@example.com", "pw")
        Membership.objects.create(workspace=self.workspace, user=bob, role=Membership.ROLE_MEMBER)
        PersonalAccessToken.issue(user=bob, workspace=self.workspace, wallet=self.wallet, name="De Bob")

        self.client.force_authenticate(self.user)
        resp = self.client.get(self.LIST_URL, **self._headers())
        self.assertEqual(resp.data["count"], 0)

    def test_revoke_deletes_token(self):
        self.client.force_authenticate(self.user)
        created = self.client.post(
            self.LIST_URL, {"name": "x", "wallet": str(self.wallet.id)}, **self._headers()
        )
        token_id = created.data["id"]

        delete_resp = self.client.delete(f"{self.LIST_URL}{token_id}/", **self._headers())
        self.assertEqual(delete_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PersonalAccessToken.objects.filter(id=token_id).exists())
