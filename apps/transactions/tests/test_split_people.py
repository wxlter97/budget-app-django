"""Dividir una transacción entre varias personas (Person/TransactionShare) --
distinto de `split` (que divide el MONTO entre categorías, ver test_split.py)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Person, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class SplitPeopleTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        cls.membership = Membership.objects.create(
            workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER
        )
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING,
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("90.00"),
            description="Cena", date="2026-02-01", type=Transaction.TYPE_EXPENSE,
        )
        self.friend = Person.objects.create(workspace=self.ws, name="Beto")

    def test_split_between_people_creates_shares_and_defaults_paid_by_to_me(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(len(resp.data["shares"]), 1)
        self.assertEqual(str(resp.data["shares"][0]["person"]), str(self.friend.id))
        self.assertEqual(Decimal(resp.data["shares"][0]["amount"]), Decimal("30.00"))

        me = Person.objects.get(workspace=self.ws, member=self.membership)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.paid_by_id, me.id)

    def test_shares_cannot_exceed_transaction_amount(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "200.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_calling_split_people_again_replaces_previous_shares(self):
        self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        other_friend = Person.objects.create(workspace=self.ws, name="Carla")
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(other_friend.id), "amount": "45.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["shares"]), 1)
        self.assertEqual(str(resp.data["shares"][0]["person"]), str(other_friend.id))

    def test_cannot_split_people_on_a_transfer(self):
        other_wallet = Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS
        )
        transfer = Transaction.objects.create(
            wallet=self.wallet, to_wallet=other_wallet, amount=Decimal("50.00"),
            date="2026-02-02", type=Transaction.TYPE_TRANSFER,
        )
        resp = self.client.post(
            f"/api/v1/transactions/{transfer.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "10.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_person_from_another_workspace(self):
        other_ws = Workspace.objects.create(name="Otro")
        foreign_person = Person.objects.create(workspace=other_ws, name="Ajeno")
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(foreign_person.id), "amount": "10.00"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_settle_share_marks_it_settled(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        share_id = resp.data["shares"][0]["id"]
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/settle-share/{share_id}/",
            {"is_settled": True},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data["shares"][0]["is_settled"])

    def test_balances_endpoint_nets_debts_between_two_people(self):
        # Yo pago 90, Beto me debe 30.
        self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        # Beto paga otra de 20, yo le debo 8 -- neto: yo le debo a Beto 8 - 30 = -22,
        # o sea Beto sigue debiéndome 22 en total.
        other_txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("20.00"),
            date="2026-02-03", type=Transaction.TYPE_EXPENSE,
        )
        me = Person.objects.get(workspace=self.ws, member=self.membership)
        self.client.post(
            f"/api/v1/transactions/{other_txn.id}/split-people/",
            {"paid_by": str(self.friend.id), "participants": [{"person": str(me.id), "amount": "8.00"}]},
            format="json",
        )

        resp = self.client.get("/api/v1/transactions/balances/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        row = resp.data[0]
        self.assertEqual(str(row["from_person"]["id"]), str(self.friend.id))
        self.assertEqual(str(row["to_person"]["id"]), str(me.id))
        self.assertEqual(Decimal(row["amount"]), Decimal("22.00"))

    def test_settled_shares_excluded_from_balances(self):
        resp = self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        share_id = resp.data["shares"][0]["id"]
        self.client.post(
            f"/api/v1/transactions/{self.txn.id}/settle-share/{share_id}/",
            {"is_settled": True},
            format="json",
        )
        resp = self.client.get("/api/v1/transactions/balances/")
        self.assertEqual(resp.data, [])

    def test_settle_balance_clears_all_shares_between_a_pair(self):
        # Dos cenas distintas que Beto me debe -- "saldar" el par tiene que
        # liquidar las dos de una, no sólo una.
        other_txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("50.00"),
            date="2026-02-04", type=Transaction.TYPE_EXPENSE,
        )
        self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        self.client.post(
            f"/api/v1/transactions/{other_txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "10.00"}]},
            format="json",
        )
        me = Person.objects.get(workspace=self.ws, member=self.membership)

        resp = self.client.post(
            "/api/v1/transactions/settle-balance/",
            {"from_person": str(self.friend.id), "to_person": str(me.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data, [])

        balances_resp = self.client.get("/api/v1/transactions/balances/")
        self.assertEqual(balances_resp.data, [])

    def test_settle_balance_rejects_person_from_another_workspace(self):
        other_ws = Workspace.objects.create(name="Otro")
        foreign_person = Person.objects.create(workspace=other_ws, name="Ajeno")
        me = Person.objects.get_or_create(
            workspace=self.ws, member=self.membership, defaults={"name": "Yo"}
        )[0]
        resp = self.client.post(
            "/api/v1/transactions/settle-balance/",
            {"from_person": str(foreign_person.id), "to_person": str(me.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_person_blocked_when_they_have_shares(self):
        self.client.post(
            f"/api/v1/transactions/{self.txn.id}/split-people/",
            {"participants": [{"person": str(self.friend.id), "amount": "30.00"}]},
            format="json",
        )
        resp = self.client.delete(f"/api/v1/people/{self.friend.id}/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Person.objects.filter(id=self.friend.id).exists())

    def test_delete_person_allowed_when_unused(self):
        unused = Person.objects.create(workspace=self.ws, name="Sin transacciones")
        resp = self.client.delete(f"/api/v1/people/{unused.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Person.objects.filter(id=unused.id).exists())
