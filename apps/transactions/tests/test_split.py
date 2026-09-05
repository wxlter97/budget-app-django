"""Dividir una transacción en varias partes (categorías/montos distintos)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class SplitTransactionTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Cuenta", purpose=Wallet.PURPOSE_SPENDING,
            opening_balance=Decimal("500.00"),
        )
        cls.food = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.hygiene = Category.objects.create(
            workspace=cls.ws, name="Higiene", type=Category.TYPE_EXPENSE
        )
        cls.income_cat = Category.objects.create(
            workspace=cls.ws, name="Sueldo", type=Category.TYPE_INCOME
        )

    def setUp(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(**{HEADER: str(self.ws.id)})
        self.txn = Transaction.objects.create(
            wallet=self.wallet, category=self.food, amount=Decimal("100.00"),
            description="Super", date="2026-01-15", type=Transaction.TYPE_EXPENSE,
        )

    def _split(self, txn_id, parts):
        # format="json": el default (multipart) no sabe mandar una lista de
        # dicts anidada.
        return self.client.post(
            f"/api/v1/transactions/{txn_id}/split/", {"parts": parts}, format="json"
        )

    def test_split_replaces_transaction_with_parts(self):
        resp = self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(len(resp.data), 2)

        self.assertFalse(Transaction.objects.filter(id=self.txn.id).exists())
        group = resp.data[0]["split_group"]
        self.assertIsNotNone(group)
        parts = Transaction.objects.filter(split_group=group).order_by("amount")
        self.assertEqual(list(parts.values_list("amount", "category_id")), [
            (Decimal("30.00"), self.hygiene.id),
            (Decimal("70.00"), self.food.id),
        ])

    def test_wallet_balance_unchanged_after_split(self):
        self.wallet.refresh_from_db()
        before = self.wallet.current_balance
        self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.current_balance, before)

    def test_parts_must_sum_to_original_amount(self):
        resp = self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "40.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Transaction.objects.filter(id=self.txn.id).exists())

    def test_requires_at_least_two_parts(self):
        resp = self._split(self.txn.id, [{"category": str(self.food.id), "amount": "100.00"}])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_category_of_wrong_type(self):
        resp = self._split(
            self.txn.id,
            [
                {"category": str(self.income_cat.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_category_from_another_workspace(self):
        other_ws = Workspace.objects.create(name="Otro")
        foreign_cat = Category.objects.create(
            workspace=other_ws, name="Ajena", type=Category.TYPE_EXPENSE
        )
        resp = self._split(
            self.txn.id,
            [
                {"category": str(foreign_cat.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_split_a_transfer(self):
        other_wallet = Wallet.objects.create(
            workspace=self.ws, name="Ahorro", purpose=Wallet.PURPOSE_SAVINGS
        )
        transfer = Transaction.objects.create(
            wallet=self.wallet, to_wallet=other_wallet, amount=Decimal("50.00"),
            date="2026-01-16", type=Transaction.TYPE_TRANSFER,
        )
        resp = self._split(
            transfer.id,
            [
                {"category": str(self.food.id), "amount": "25.00"},
                {"category": str(self.hygiene.id), "amount": "25.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_split_an_already_split_transaction(self):
        self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        part = Transaction.objects.filter(category=self.food).first()
        resp = self._split(
            part.id,
            [
                {"category": str(self.food.id), "amount": "50.00"},
                {"category": str(self.hygiene.id), "amount": "20.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_one_part_clears_split_group_on_the_last_remaining(self):
        self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        hygiene_part = Transaction.objects.get(category=self.hygiene)
        food_part = Transaction.objects.get(category=self.food)

        resp = self.client.delete(f"/api/v1/transactions/{hygiene_part.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        food_part.refresh_from_db()
        self.assertIsNone(food_part.split_group)

    def test_filter_by_split_group(self):
        created = self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        ).data
        group = created[0]["split_group"]
        resp = self.client.get(f"/api/v1/transactions/?split_group={group}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["count"], 2)

    def test_custom_description_per_part(self):
        resp = self._split(
            self.txn.id,
            [
                {"category": str(self.food.id), "amount": "70.00", "description": "Verduras"},
                {"category": str(self.hygiene.id), "amount": "30.00"},
            ],
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        by_cat = {str(p["category"]): p["description"] for p in resp.data}
        self.assertEqual(by_cat[str(self.food.id)], "Verduras")
        self.assertEqual(by_cat[str(self.hygiene.id)], "Super")  # hereda la original
