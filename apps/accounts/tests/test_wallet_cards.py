"""WalletCard: plásticos adicionales de una misma cuenta (titular +
adicionales comparten saldo/límite/estado de cuenta, ver docstring del
modelo) -- y su exposición en el WalletSerializer (`extra_cards`)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet, WalletCard
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class WalletCardModelTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws = Workspace.objects.create(name="W")
        cls.card = Wallet.objects.create(
            workspace=cls.ws, name="Tarjeta", kind=Wallet.KIND_CREDIT,
            card_last4="8303", credit_limit=Decimal("2000.00"),
        )

    def test_duplicate_last4_on_same_wallet_rejected(self):
        WalletCard.objects.create(wallet=self.card, last4="9126")
        with self.assertRaises(IntegrityError):
            WalletCard.objects.create(wallet=self.card, last4="9126")


class WalletExtraCardsApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.user)
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Cuscatlán UNO", kind=Wallet.KIND_CREDIT,
            card_last4="8303", credit_limit=Decimal("2000.00"),
        )

    def test_create_wallet_with_extra_cards(self):
        res = self.client.post(
            "/api/v1/wallets/",
            {
                "name": "Otra", "purpose": Wallet.PURPOSE_SPENDING, "kind": Wallet.KIND_CREDIT,
                "card_last4": "1111",
                "extra_cards": [{"last4": "2222", "label": "Adicional"}],
            },
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(len(res.data["extra_cards"]), 1)
        self.assertEqual(res.data["extra_cards"][0]["last4"], "2222")
        self.assertEqual(res.data["extra_cards"][0]["label"], "Adicional")

    def test_patch_extra_cards_replaces_the_whole_list(self):
        WalletCard.objects.create(wallet=self.card, last4="9126", label="Adicional")

        res = self.client.patch(
            f"/api/v1/wallets/{self.card.id}/",
            {"extra_cards": [{"last4": "5858", "label": "Otra más"}]},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        last4s = {c["last4"] for c in res.data["extra_cards"]}
        self.assertEqual(last4s, {"5858"})  # la 9126 vieja se reemplazó, no se acumuló

    def test_patch_without_extra_cards_key_leaves_them_untouched(self):
        WalletCard.objects.create(wallet=self.card, last4="9126")

        res = self.client.patch(
            f"/api/v1/wallets/{self.card.id}/",
            {"name": "Renombrada"},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual({c["last4"] for c in res.data["extra_cards"]}, {"9126"})

    def test_extra_card_last4_must_be_4_digits(self):
        res = self.client.patch(
            f"/api/v1/wallets/{self.card.id}/",
            {"extra_cards": [{"last4": "12"}]},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
