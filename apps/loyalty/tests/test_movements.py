"""Canjes y ajustes: el disponible de una tarjeta es `ganado + movimientos`, y lo único
que se edita es el libro de movimientos (lo ganado sale de los gastos)."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.loyalty import services
from apps.loyalty.models import (
    Bank,
    CardProduct,
    LoyaltyEarning,
    LoyaltyMovement,
    LoyaltyProgram,
)
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
D = Decimal


class Base(APITestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        bank = Bank.objects.create(name="Banco X")
        self.product = CardProduct.objects.create(bank=bank, name="Signature")
        self.points = LoyaltyProgram.objects.create(
            card_product=self.product, kind="points", name="Puntos", default_rate=D("1"),
            point_value=D("0.01"),
        )
        self.cashback = LoyaltyProgram.objects.create(
            card_product=self.product, kind="cashback", name="Cashback", default_rate=D("0.02"),
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=D("3000"), card_product=self.product,
        )
        self.bank_wallet = Wallet.objects.create(workspace=self.ws, name="Cuenta", kind=Wallet.KIND_BANK)
        self.cat = Category.objects.create(workspace=self.ws, name="X", type=Category.TYPE_EXPENSE)
        # 1000 puntos y $20 de cashback ganados
        Transaction.objects.create(wallet=self.card, category=self.cat, amount=D("1000"), date="2026-09-01")

    def kw(self, program=None, **extra):
        return dict(workspace=self.ws, wallet=self.card, program=program or self.points, user=self.user, **extra)

    def available(self, program=None):
        return services.available_for(self.ws, self.card.id, (program or self.points).id)


class BalanceTests(Base):
    def test_available_is_earned_plus_adjustments_minus_redemptions(self):
        self.assertEqual(self.available(), D("1000"))
        services.adjust(quantity=D("50"), **self.kw())
        services.redeem(quantity=D("300"), **self.kw())
        self.assertEqual(self.available(), D("750"))
        card = services.wallet_balances(self.ws)[0]
        points = next(p for p in card["programs"] if p["kind"] == "points")
        self.assertEqual((points["earned"], points["adjusted"], points["redeemed"], points["available"]),
                         (D("1000"), D("50"), D("300"), D("750")))
        self.assertEqual(points["estimated_value"], D("7.50"))

    def test_the_overview_is_one_entry_per_card_with_its_bank_and_total(self):
        other_bank = Bank.objects.create(name="Otro banco")
        other_product = CardProduct.objects.create(bank=other_bank, name="Oro")
        LoyaltyProgram.objects.create(card_product=other_product, kind="cashback", default_rate=D("0.01"))
        Wallet.objects.create(workspace=self.ws, name="Oro", kind=Wallet.KIND_CREDIT,
                              credit_limit=D("100"), card_product=other_product)
        cards = services.wallet_balances(self.ws)
        self.assertEqual([c["bank_name"] for c in cards], ["Banco X", "Otro banco"])
        # 1000 pts x 0.01 + $20 de cashback
        self.assertEqual(cards[0]["total_value"], D("30.00"))
        self.assertEqual(cards[1]["total_value"], D("0"))

    def test_a_wallet_without_a_product_is_not_listed(self):
        self.assertEqual([c["wallet"] for c in services.wallet_balances(self.ws)], [self.card.id])

    def test_movements_do_not_disappear_when_earnings_are_recomputed(self):
        services.redeem(quantity=D("400"), **self.kw())
        services.recompute_earnings(Transaction.objects.all())
        self.assertEqual(self.available(), D("600"))


class RedeemTests(Base):
    def test_cannot_redeem_more_than_available_or_nothing(self):
        for qty in (D("1000.01"), D("0"), D("-5")):
            with self.assertRaises(ValidationError):
                services.redeem(quantity=qty, **self.kw())
        self.assertEqual(LoyaltyMovement.objects.count(), 0)

    def test_redeeming_everything_is_allowed(self):
        services.redeem(quantity=D("1000"), **self.kw())
        self.assertEqual(self.available(), D("0"))

    def test_cashback_redeem_deposits_the_same_amount(self):
        self.assertEqual(self.available(self.cashback), D("20.00"))
        m = services.redeem(quantity=D("15"), deposit_wallet=self.bank_wallet, **self.kw(self.cashback))
        self.assertEqual(m.cash_value, D("15"))
        txn = m.deposit_transaction
        self.assertEqual((txn.type, txn.amount, txn.wallet_id), ("income", D("15"), self.bank_wallet.id))
        self.assertEqual(txn.category.name, "Recompensas")

    def test_points_use_the_program_value_unless_a_value_is_given(self):
        m = services.redeem(quantity=D("200"), deposit_wallet=self.bank_wallet, **self.kw())
        self.assertEqual(m.cash_value, D("2.00"))  # 200 x 0.01
        m2 = services.redeem(quantity=D("200"), cash_value=D("2.50"), deposit_wallet=self.bank_wallet, **self.kw())
        self.assertEqual(m2.deposit_transaction.amount, D("2.50"))

    def test_depositing_points_without_any_value_asks_for_one(self):
        self.points.point_value = None
        self.points.save()
        with self.assertRaises(ValidationError):
            services.redeem(quantity=D("100"), deposit_wallet=self.bank_wallet, **self.kw())
        # sin depositar sí se puede: sólo baja el disponible
        m = services.redeem(quantity=D("100"), **self.kw())
        self.assertIsNone(m.deposit_transaction)

    def test_the_deposit_wallet_must_belong_to_the_workspace(self):
        other = Wallet.objects.create(workspace=Workspace.objects.create(name="Otro"), name="Ajena", kind=Wallet.KIND_BANK)
        with self.assertRaises(ValidationError):
            services.redeem(quantity=D("10"), deposit_wallet=other, **self.kw())

    def test_a_program_of_another_card_or_a_discount_is_rejected(self):
        other = CardProduct.objects.create(bank=self.product.bank, name="Otra")
        foreign = LoyaltyProgram.objects.create(card_product=other, kind="points", default_rate=D("1"))
        discount = LoyaltyProgram.objects.create(card_product=self.product, kind="discount", default_rate=D("0.05"))
        for program in (foreign, discount):
            with self.assertRaises(ValidationError):
                services.redeem(quantity=D("1"), **self.kw(program))

    def test_a_deposit_does_not_earn_points_itself(self):
        before = LoyaltyEarning.objects.count()
        services.redeem(quantity=D("100"), deposit_wallet=self.card, **self.kw())
        self.assertEqual(LoyaltyEarning.objects.count(), before)


class AdjustTests(Base):
    def test_adjust_adds_or_subtracts_and_keeps_the_reason(self):
        services.adjust(quantity=D("250"), note="Mi banco dice 1,250", **self.kw())
        services.adjust(quantity=D("-100"), note="Puntos vencidos", **self.kw())
        self.assertEqual(self.available(), D("1150"))
        self.assertEqual(LoyaltyMovement.objects.filter(kind="adjust").count(), 2)

    def test_cannot_adjust_by_zero_or_below_zero(self):
        for qty in (D("0"), D("-1000.01")):
            with self.assertRaises(ValidationError):
                services.adjust(quantity=qty, **self.kw())


class UpdateAndUndoTests(Base):
    def test_note_and_date_can_be_edited_on_a_redeem_but_not_its_quantity(self):
        m = services.redeem(quantity=D("100"), **self.kw())
        services.update_movement(m, note="Vuelo", date="2026-09-10")
        m.refresh_from_db()
        self.assertEqual((m.note, str(m.date)), ("Vuelo", "2026-09-10"))
        with self.assertRaises(ValidationError):
            services.update_movement(m, quantity=D("50"))

    def test_an_adjustment_quantity_can_be_edited_unless_it_breaks_later_redemptions(self):
        adj = services.adjust(quantity=D("500"), **self.kw())
        services.redeem(quantity=D("1400"), **self.kw())  # gasta de lo ajustado
        services.update_movement(adj, quantity=D("450"))
        self.assertEqual(self.available(), D("50"))
        with self.assertRaises(ValidationError):
            services.update_movement(adj, quantity=D("100"))  # dejaría -300

    def test_undoing_a_deposited_redeem_removes_its_income_and_restores_the_balance(self):
        m = services.redeem(quantity=D("15"), deposit_wallet=self.bank_wallet, **self.kw(self.cashback))
        txn = m.deposit_transaction
        services.undo_movement(m)
        txn.refresh_from_db(); m.refresh_from_db()
        self.assertTrue(txn.is_deleted and m.is_deleted)
        self.assertEqual(self.available(self.cashback), D("20.00"))

    def test_cannot_undo_a_positive_adjustment_that_was_already_redeemed(self):
        adj = services.adjust(quantity=D("500"), **self.kw())
        services.redeem(quantity=D("1400"), **self.kw())
        with self.assertRaises(ValidationError):
            services.undo_movement(adj)

    def test_undoing_a_negative_adjustment_gives_the_points_back(self):
        adj = services.adjust(quantity=D("-100"), **self.kw())
        services.undo_movement(adj)
        self.assertEqual(self.available(), D("1000"))


@patch("apps.billing.services.has_feature_for_workspace", return_value=True)
class MovementApiTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _post(self, **body):
        base = {"wallet": str(self.card.pk), "program": str(self.points.pk)}
        return self.client.post("/api/v1/loyalty-movements/", {**base, **body},
                                HTTP_X_WORKSPACE_ID=str(self.ws.pk), format="json")

    def test_redeem_with_deposit_end_to_end(self, _):
        res = self._post(kind="redeem", quantity="200", deposit_wallet=str(self.bank_wallet.pk),
                         cash_value="2.10", note="Compra")
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual((body["delta"], body["cash_value"]), ("-200.00", "2.10"))
        self.assertIsNotNone(body["deposit_transaction"])
        summary = self.client.get("/api/v1/loyalty-earnings/summary/", HTTP_X_WORKSPACE_ID=str(self.ws.pk)).json()
        points = next(p for p in summary["wallets"][0]["programs"] if p["kind"] == "points")
        self.assertEqual((points["available"], points["redeemed"]), ("800.00", "200.00"))
        self.assertEqual(summary["points_balances"][0]["points"], "800.00")

    def test_errors_come_back_as_400_with_a_message(self, _):
        res = self._post(kind="redeem", quantity="5000")
        self.assertEqual(res.status_code, 400)
        self.assertIn("quantity", res.json())

    def test_an_adjustment_cannot_carry_a_deposit(self, _):
        res = self._post(kind="adjust", quantity="10", deposit_wallet=str(self.bank_wallet.pk))
        self.assertEqual(res.status_code, 400)

    def test_list_filter_patch_and_delete(self, _):
        mid = self._post(kind="adjust", quantity="25", note="ajuste").json()["id"]
        self._post(kind="redeem", quantity="10")
        url = "/api/v1/loyalty-movements/"
        h = {"HTTP_X_WORKSPACE_ID": str(self.ws.pk)}
        rows = self.client.get(url, {"kind": "adjust"}, **h).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)
        res = self.client.patch(f"{url}{mid}/", {"quantity": "40", "note": "otro"}, format="json", **h)
        self.assertEqual((res.status_code, res.json()["delta"], res.json()["note"]), (200, "40.00", "otro"))
        self.assertEqual(self.client.delete(f"{url}{mid}/", **h).status_code, 204)
        self.assertEqual(self.available(), D("990"))

    def test_movements_of_another_workspace_are_not_visible_or_editable(self, _):
        mid = self._post(kind="adjust", quantity="25").json()["id"]
        other_ws = Workspace.objects.create(name="Otro")
        outsider = User.objects.create_user("bo", "bo@example.com", "pw")
        Membership.objects.create(workspace=other_ws, user=outsider, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(outsider)
        res = self.client.get(f"/api/v1/loyalty-movements/{mid}/", HTTP_X_WORKSPACE_ID=str(other_ws.pk))
        self.assertEqual(res.status_code, 404)
        # y no se puede canjear de una cartera ajena aunque se conozca su id
        res = self._post(kind="adjust", quantity="5")
        self.assertIn(res.status_code, (400, 403, 404))

    def test_earnings_can_be_filtered_by_wallet_and_show_the_purchase(self, _):
        res = self.client.get("/api/v1/loyalty-earnings/", {"wallet": str(self.card.pk)},
                              HTTP_X_WORKSPACE_ID=str(self.ws.pk)).json()
        rows = res["results"] if isinstance(res, dict) else res
        self.assertTrue(rows)
        self.assertIn("transaction_date", rows[0])
        self.assertIn("transaction_description", rows[0])

    def test_requires_the_loyalty_plan(self, mocked):
        mocked.return_value = False
        res = self.client.get("/api/v1/loyalty-movements/", HTTP_X_WORKSPACE_ID=str(self.ws.pk))
        self.assertEqual(res.status_code, 403)
