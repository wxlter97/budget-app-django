"""Programas de lealtad: tasas por rubro, generación automática de puntos y
cashback vía señal, descuento aplicado a mano por el cliente, y el resumen
agregado (ver `apps.loyalty.services.loyalty_summary`)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
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
from apps.loyalty.services import loyalty_summary
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"


class LoyaltyProgramRateForTests(APITestCase):
    def setUp(self):
        bank = Bank.objects.create(name="Banco X")
        self.product = CardProduct.objects.create(bank=bank, name="Signature")
        self.super_type = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.other_type = CategoryType.objects.create(slug="otros", name="Otros")
        self.program = LoyaltyProgram.objects.create(
            card_product=self.product, kind=LoyaltyProgram.KIND_CASHBACK,
            default_rate=Decimal("0.01"),
        )

    def test_uses_default_rate_without_override(self):
        self.assertEqual(self.program.rate_for(self.other_type), Decimal("0.01"))

    def test_category_override_replaces_default(self):
        LoyaltyCategoryRate.objects.create(
            program=self.program, category_type=self.super_type, rate=Decimal("0.05")
        )
        self.assertEqual(self.program.rate_for(self.super_type), Decimal("0.05"))
        self.assertEqual(self.program.rate_for(self.other_type), Decimal("0.01"))

    def test_no_category_type_uses_default(self):
        self.assertEqual(self.program.rate_for(None), Decimal("0.01"))


class LoyaltyEarningSignalTests(APITestCase):
    """Puntos y cashback se generan/actualizan/borran solos con la
    Transaction -- ver `apps.loyalty.signals`."""

    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        bank = Bank.objects.create(name="Banco X")
        self.product = CardProduct.objects.create(bank=bank, name="Signature")
        self.super_type = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.points = LoyaltyProgram.objects.create(
            card_product=self.product, kind=LoyaltyProgram.KIND_POINTS, default_rate=Decimal("2"),
        )
        self.cashback = LoyaltyProgram.objects.create(
            card_product=self.product, kind=LoyaltyProgram.KIND_CASHBACK, default_rate=Decimal("0.01"),
        )
        LoyaltyCategoryRate.objects.create(
            program=self.cashback, category_type=self.super_type, rate=Decimal("0.05")
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=self.product,
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Super", type=Category.TYPE_EXPENSE,
            category_type=self.super_type,
        )

    def _spend(self, amount="100.00"):
        return Transaction.objects.create(
            wallet=self.card, category=self.cat, amount=Decimal(amount), date="2026-09-01",
        )

    def test_creates_points_and_cashback_with_category_override(self):
        txn = self._spend("100.00")
        earnings = {e.kind: e for e in LoyaltyEarning.objects.filter(transaction=txn)}
        self.assertEqual(earnings[LoyaltyProgram.KIND_POINTS].points, Decimal("200.00"))
        # 5% override de "Supermercado", no el 1% default del programa.
        self.assertEqual(earnings[LoyaltyProgram.KIND_CASHBACK].amount, Decimal("5.00"))

    def test_no_card_product_earns_nothing(self):
        plain = Wallet.objects.create(workspace=self.ws, name="Efectivo", kind=Wallet.KIND_CASH)
        txn = Transaction.objects.create(
            wallet=plain, category=self.cat, amount=Decimal("50.00"), date="2026-09-01",
        )
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn).exists())

    def test_uncategorized_category_type_earns_nothing(self):
        other_cat = Category.objects.create(
            workspace=self.ws, name="Otro", type=Category.TYPE_EXPENSE,
        )
        txn = Transaction.objects.create(
            wallet=self.card, category=other_cat, amount=Decimal("50.00"), date="2026-09-01",
        )
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn).exists())

    def test_income_does_not_earn(self):
        income_cat = Category.objects.create(
            workspace=self.ws, name="Sueldo", type=Category.TYPE_INCOME,
            category_type=self.super_type,
        )
        txn = Transaction.objects.create(
            wallet=self.card, category=income_cat, amount=Decimal("50.00"), date="2026-09-01",
        )
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn).exists())

    def test_editing_amount_recomputes_earnings(self):
        txn = self._spend("100.00")
        txn.amount = Decimal("200.00")
        txn.save()
        cashback = LoyaltyEarning.objects.get(transaction=txn, kind=LoyaltyProgram.KIND_CASHBACK)
        self.assertEqual(cashback.amount, Decimal("10.00"))

    def test_soft_delete_removes_earnings(self):
        txn = self._spend()
        txn.soft_delete()
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn).exists())

    def test_hard_delete_removes_earnings(self):
        txn = self._spend()
        txn_id = txn.id
        txn.delete()
        self.assertFalse(LoyaltyEarning.objects.filter(transaction_id=txn_id).exists())


class DiscountViaTransactionApiTests(APITestCase):
    """El descuento lo aplica el cliente al crear la transacción (botón
    "aplicar descuento" del formulario) -- ver `TransactionSerializer`."""

    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        bank = Bank.objects.create(name="Banco X")
        self.product = CardProduct.objects.create(bank=bank, name="Signature")
        other_bank_product = CardProduct.objects.create(bank=bank, name="Otra")
        self.discount = LoyaltyProgram.objects.create(
            card_product=self.product, kind=LoyaltyProgram.KIND_DISCOUNT, default_rate=Decimal("0.15"),
        )
        self.other_discount = LoyaltyProgram.objects.create(
            card_product=other_bank_product, kind=LoyaltyProgram.KIND_DISCOUNT, default_rate=Decimal("0.10"),
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), card_product=self.product,
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Restaurantes", type=Category.TYPE_EXPENSE,
        )
        self.client.force_authenticate(self.user)

    def _create(self, **over):
        payload = {
            "wallet": str(self.card.id), "category": str(self.cat.id),
            "amount": "85.00", "date": "2026-09-01", "description": "Cena",
            **over,
        }
        return self.client.post("/api/v1/transactions/", payload, **{HEADER: str(self.ws.id)})

    def test_applying_discount_records_original_amount_and_savings(self):
        res = self._create(discount_program=str(self.discount.id), pre_discount_amount="100.00")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        earning = LoyaltyEarning.objects.get(program=self.discount)
        self.assertEqual(earning.kind, LoyaltyProgram.KIND_DISCOUNT)
        self.assertEqual(earning.original_amount, Decimal("100.00"))
        self.assertEqual(earning.discount_saved_amount, Decimal("15.00"))
        self.assertEqual(res.data["loyalty_earnings"][0]["saved_amount"], Decimal("15.00"))

    def test_without_discount_fields_no_earning_created(self):
        res = self._create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(LoyaltyEarning.objects.exists())

    def test_requires_both_fields_together(self):
        res = self._create(pre_discount_amount="100.00")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_program_from_another_card_product(self):
        res = self._create(discount_program=str(self.other_discount.id), pre_discount_amount="100.00")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_pre_discount_amount_below_final_amount(self):
        res = self._create(discount_program=str(self.discount.id), pre_discount_amount="80.00")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class WalletCardProductAndCategoryTypeTests(APITestCase):
    """Enlaces del catálogo con lo del workspace: `Wallet.card_product` (sólo
    tarjetas de crédito) y `Category.category_type` (cualquier categoría)."""

    def setUp(self):
        self.user = User.objects.create_user("u", "u@e.com", "pw")
        self.ws = Workspace.objects.create(name="W")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        bank = Bank.objects.create(name="Banco X")
        self.product = CardProduct.objects.create(bank=bank, name="Signature")
        self.super_type = CategoryType.objects.create(slug="super", name="Super")
        self.client.force_authenticate(self.user)

    def test_credit_wallet_can_set_card_product(self):
        res = self.client.post(
            "/api/v1/wallets/",
            {"name": "Visa", "kind": Wallet.KIND_CREDIT, "card_product": str(self.product.id)},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["card_product_name"], "Signature")
        self.assertEqual(res.data["card_bank_name"], "Banco X")

    def test_non_credit_wallet_rejects_card_product(self):
        res = self.client.post(
            "/api/v1/wallets/",
            {"name": "Efectivo", "kind": Wallet.KIND_CASH, "card_product": str(self.product.id)},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_category_can_map_to_a_category_type(self):
        res = self.client.post(
            "/api/v1/categories/",
            {"name": "Super", "type": Category.TYPE_EXPENSE, "category_type": str(self.super_type.id)},
            **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["category_type"], self.super_type.id)


class LoyaltySummaryServiceTests(APITestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        bank = Bank.objects.create(name="Banco X")
        product = CardProduct.objects.create(bank=bank, name="Signature")
        self.super_type = CategoryType.objects.create(slug="super", name="Super")
        self.points = LoyaltyProgram.objects.create(
            card_product=product, kind=LoyaltyProgram.KIND_POINTS,
            default_rate=Decimal("2"), point_value=Decimal("0.01"), name="MillasX",
        )
        self.cashback = LoyaltyProgram.objects.create(
            card_product=product, kind=LoyaltyProgram.KIND_CASHBACK, default_rate=Decimal("0.02"),
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), card_product=product,
        )
        self.cat = Category.objects.create(
            workspace=self.ws, name="Super", type=Category.TYPE_EXPENSE,
            category_type=self.super_type,
        )

    def _spend(self, amount, date):
        return Transaction.objects.create(
            wallet=self.card, category=self.cat, amount=Decimal(amount), date=date,
        )

    def test_points_balance_and_estimated_value(self):
        self._spend("100.00", "2026-08-01")
        self._spend("50.00", "2026-09-01")
        summary = loyalty_summary(self.ws)
        balance = next(b for b in summary["points_balances"] if b["program"] == self.points.id)
        self.assertEqual(balance["points"], Decimal("300.00"))
        self.assertEqual(balance["estimated_value"], Decimal("3.0000"))

    def test_period_totals_filters_by_date(self):
        self._spend("100.00", "2026-08-01")  # 2.00 cashback, fuera del período
        self._spend("50.00", "2026-09-05")   # 1.00 cashback, dentro
        summary = loyalty_summary(self.ws, date_after="2026-09-01", date_before="2026-09-30")
        totals = summary["period_totals"][0]
        self.assertEqual(totals["cashback_earned"], Decimal("1.00"))
