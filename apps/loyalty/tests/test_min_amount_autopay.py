"""Compra mínima del programa ("cashback a partir de $10") y tasas que sólo valen
para cargos automáticos (Pagos Automáticos de servicios)."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.loyalty.models import (
    Bank,
    CardProduct,
    CategoryType,
    LoyaltyCategoryRate,
    LoyaltyEarning,
    LoyaltyProgram,
)
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Workspace


class MinAmountTests(APITestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        product = CardProduct.objects.create(bank=Bank.objects.create(name="B"), name="T")
        self.cashback = LoyaltyProgram.objects.create(
            card_product=product, kind="cashback", default_rate=Decimal("0.05"),
            min_amount=Decimal("10"),
        )
        self.points = LoyaltyProgram.objects.create(
            card_product=product, kind="points", default_rate=Decimal("1"),
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=product,
        )
        self.cat = Category.objects.create(workspace=self.ws, name="X", type=Category.TYPE_EXPENSE)

    def _spend(self, amount):
        txn = Transaction.objects.create(
            wallet=self.card, category=self.cat, amount=Decimal(amount), date="2026-09-22"
        )
        return {e.kind: e for e in LoyaltyEarning.objects.filter(transaction=txn)}

    def test_qualifies_is_inclusive_and_optional(self):
        self.assertTrue(self.cashback.qualifies(Decimal("10.00")))
        self.assertTrue(self.cashback.qualifies(Decimal("10.01")))
        self.assertFalse(self.cashback.qualifies(Decimal("9.99")))
        self.assertTrue(self.points.qualifies(Decimal("0.01")))  # sin mínimo

    def test_below_the_minimum_the_program_earns_nothing_but_the_others_still_do(self):
        earned = self._spend("9.99")
        self.assertNotIn("cashback", earned)
        self.assertEqual(earned["points"].points, Decimal("9.99"))

    def test_at_the_minimum_it_earns(self):
        self.assertEqual(self._spend("10.00")["cashback"].amount, Decimal("0.50"))

    def test_editing_the_amount_across_the_minimum_adds_and_removes_the_earning(self):
        txn = Transaction.objects.create(
            wallet=self.card, category=self.cat, amount=Decimal("5.00"), date="2026-09-22"
        )
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn, kind="cashback").exists())
        txn.amount = Decimal("20.00")
        txn.save()
        self.assertEqual(LoyaltyEarning.objects.get(transaction=txn, kind="cashback").amount, Decimal("1.00"))
        txn.amount = Decimal("3.00")
        txn.save()
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn, kind="cashback").exists())


class AutopayRateTests(APITestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.agua = CategoryType.objects.create(slug="agua", name="Agua")
        product = CardProduct.objects.create(bank=Bank.objects.create(name="B"), name="ePay")
        self.program = LoyaltyProgram.objects.create(
            card_product=product, kind="cashback", default_rate=Decimal("0.01"),
        )
        LoyaltyCategoryRate.objects.create(
            program=self.program, category_type=self.agua, rate=Decimal("0.05"), requires_autopay=True
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="ePay", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=product,
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Agua", type=Category.TYPE_EXPENSE, category_type=self.agua
        )

    def _spend(self, **kw):
        txn = Transaction.objects.create(
            wallet=self.card, category=self.cat, amount=Decimal("100.00"), date="2026-09-22", **kw
        )
        return LoyaltyEarning.objects.get(transaction=txn).amount

    def test_the_autopay_rate_only_applies_to_automatic_charges(self):
        self.assertEqual(self.program.rate_for(self.agua), Decimal("0.01"))
        self.assertEqual(self.program.rate_for(self.agua, autopay=True), Decimal("0.05"))

    def test_a_normal_payment_earns_the_base_rate_and_an_automatic_one_the_special(self):
        self.assertEqual(self._spend(), Decimal("1.00"))
        self.assertEqual(self._spend(is_autopay=True), Decimal("5.00"))

    def test_an_autopay_rate_does_not_change_other_categories(self):
        other = CategoryType.objects.create(slug="otro", name="Otro")
        self.assertEqual(self.program.rate_for(other, autopay=True), Decimal("0.01"))

    def test_a_program_can_have_a_normal_and_an_autopay_rate_for_the_same_rubro(self):
        LoyaltyCategoryRate.objects.create(
            program=self.program, category_type=self.agua, rate=Decimal("0.02")
        )
        self.assertEqual(self.program.rate_for(self.agua), Decimal("0.02"))
        self.assertEqual(self.program.rate_for(self.agua, autopay=True), Decimal("0.05"))

    def test_the_transaction_api_accepts_and_returns_is_autopay(self):
        from django.contrib.auth import get_user_model
        from apps.workspaces.models import Membership

        user = get_user_model().objects.create_user("ana", "ana@example.com", "pw")
        Membership.objects.create(workspace=self.ws, user=user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(user)
        res = self.client.post(
            "/api/v1/transactions/",
            {"wallet": str(self.card.pk), "category": str(self.cat.pk), "type": "expense",
             "amount": "100.00", "date": "2026-09-22", "is_autopay": True},
            HTTP_X_WORKSPACE_ID=str(self.ws.pk),
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.json()["is_autopay"])
        self.assertEqual(LoyaltyEarning.objects.get(transaction_id=res.json()["id"]).amount, Decimal("5.00"))


class CuscatlanCashbackMinimumTests(APITestCase):
    def test_every_cuscatlan_cashback_program_requires_10_dollars(self):
        call_command("seed_loyalty_catalog", stdout=StringIO())
        programs = LoyaltyProgram.objects.filter(
            card_product__bank__name="Banco Cuscatlán", kind="cashback"
        )
        self.assertGreaterEqual(programs.count(), 5)
        for program in programs:
            self.assertEqual(program.min_amount, Decimal("10.00"), program.card_product.name)

    def test_other_banks_have_no_minimum(self):
        call_command("seed_loyalty_catalog", stdout=StringIO())
        self.assertFalse(
            LoyaltyProgram.objects.exclude(card_product__bank__name="Banco Cuscatlán")
            .filter(min_amount__isnull=False).exists()
        )

    def test_epay_pays_5_percent_on_utilities_only_when_automatic(self):
        call_command("seed_loyalty_catalog", stdout=StringIO())
        program = LoyaltyProgram.objects.get(card_product__name="ePay", kind="cashback")
        agua = CategoryType.objects.get(slug="agua")
        self.assertEqual(program.rate_for(agua), Decimal("0.01"))
        self.assertEqual(program.rate_for(agua, autopay=True), Decimal("0.05"))
