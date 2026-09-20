"""Comercios: reconocerlos en la descripción, tasas de un solo comercio, y el
mapeo de categorías a rubros (`map_categories_to_rubros`)."""
from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.loyalty import catalog
from apps.loyalty.models import (
    Bank,
    CardProduct,
    CategoryType,
    LoyaltyCategoryRate,
    LoyaltyEarning,
    LoyaltyProgram,
    Merchant,
)
from apps.loyalty.services import match_merchant, normalize_text
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Workspace

User = get_user_model()


class NormalizeAndMatchTests(APITestCase):
    def setUp(self):
        self.super = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.selectos = Merchant.objects.create(
            name="Súper Selectos", category_type=self.super, aliases="selectos\nsuper selectos"
        )
        self.mc = Merchant.objects.create(name="McDonald's", aliases="mc\nmcdonalds")
        self.uber = Merchant.objects.create(name="Uber")
        self.eats = Merchant.objects.create(name="Uber Eats")

    def test_normalize_drops_accents_case_and_punctuation(self):
        self.assertEqual(normalize_text("  McDonald's — Metrocentro "), "mcdonald s metrocentro")
        self.assertEqual(normalize_text("Súper Selectos"), "super selectos")
        self.assertEqual(normalize_text(None), "")

    def test_matches_ignoring_case_and_accents(self):
        self.assertEqual(match_merchant("SUPER Selectos san miguel"), self.selectos)
        self.assertEqual(match_merchant("compras en súper selectos"), self.selectos)

    def test_only_whole_words_count(self):
        # "mc" no debe dispararse dentro de otra palabra.
        self.assertEqual(match_merchant("mc combo"), self.mc)
        self.assertIsNone(match_merchant("mcafee antivirus"))
        self.assertIsNone(match_merchant("alguno selectosa"))

    def test_the_longest_alias_wins(self):
        self.assertEqual(match_merchant("uber eats pedido"), self.eats)
        self.assertEqual(match_merchant("uber a casa"), self.uber)

    def test_blank_or_unknown_description_matches_nothing(self):
        self.assertIsNone(match_merchant(""))
        self.assertIsNone(match_merchant("   "))
        self.assertIsNone(match_merchant("pupusas"))


class MerchantRateTests(APITestCase):
    def setUp(self):
        bank = Bank.objects.create(name="Banco X")
        product = CardProduct.objects.create(bank=bank, name="Tarjeta")
        self.super = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.selectos = Merchant.objects.create(name="Súper Selectos", category_type=self.super)
        self.program = LoyaltyProgram.objects.create(
            card_product=product, kind="cashback", default_rate=Decimal("0.01")
        )

    def _rate(self, rate, weekday=None, **target):
        return LoyaltyCategoryRate.objects.create(
            program=self.program, rate=Decimal(rate), weekday=weekday, **target
        )

    def test_precedence_merchant_day_merchant_category_day_category_default(self):
        monday, tuesday = date(2026, 9, 21), date(2026, 9, 22)
        self._rate("0.02", category_type=self.super)
        self._rate("0.03", weekday=0, category_type=self.super)
        self.assertEqual(self.program.rate_for(self.super, monday, self.selectos), Decimal("0.03"))
        self._rate("0.07", merchant=self.selectos)
        self.assertEqual(self.program.rate_for(self.super, tuesday, self.selectos), Decimal("0.07"))
        self._rate("0.09", weekday=0, merchant=self.selectos)
        self.assertEqual(self.program.rate_for(self.super, monday, self.selectos), Decimal("0.09"))
        # Sin comercio se vuelve a las reglas del rubro.
        self.assertEqual(self.program.rate_for(self.super, monday), Decimal("0.03"))
        self.assertEqual(self.program.rate_for(self.super, tuesday), Decimal("0.02"))
        self.assertEqual(self.program.rate_for(None), Decimal("0.01"))

    def test_a_merchant_without_rules_falls_back_to_its_category(self):
        self._rate("0.05", category_type=self.super)
        self.assertEqual(self.program.rate_for(self.super, None, self.selectos), Decimal("0.05"))

    def test_a_rate_needs_exactly_one_of_category_type_or_merchant(self):
        for target in ({}, {"category_type": self.super, "merchant": self.selectos}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self._rate("0.05", **target)

    def test_no_duplicate_rate_for_a_merchant_and_day(self):
        for weekday in (None, 0):
            self._rate("0.05", weekday=weekday, merchant=self.selectos)
            with self.assertRaises(IntegrityError), transaction.atomic():
                self._rate("0.06", weekday=weekday, merchant=self.selectos)


class MerchantSignalTests(APITestCase):
    """El comercio de la descripción manda sobre la categoría de la transacción."""

    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        bank = Bank.objects.create(name="Banco X")
        product = CardProduct.objects.create(bank=bank, name="Tarjeta")
        self.super = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.rest = CategoryType.objects.create(slug="restaurantes", name="Restaurantes")
        self.selectos = Merchant.objects.create(
            name="Súper Selectos", category_type=self.super, aliases="selectos"
        )
        self.program = LoyaltyProgram.objects.create(
            card_product=product, kind="cashback", default_rate=Decimal("0.01")
        )
        LoyaltyCategoryRate.objects.create(
            program=self.program, category_type=self.super, rate=Decimal("0.03")
        )
        LoyaltyCategoryRate.objects.create(
            program=self.program, merchant=self.selectos, rate=Decimal("0.07")
        )
        self.card = Wallet.objects.create(
            workspace=self.ws, name="Visa", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=product,
        )
        # "Comida" está mapeada a restaurantes, como en producción.
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE, category_type=self.rest
        )

    def _spend(self, description):
        txn = Transaction.objects.create(
            wallet=self.card, category=self.food, amount=Decimal("100.00"),
            date="2026-09-22", description=description,
        )
        return LoyaltyEarning.objects.filter(transaction=txn).first()

    def test_merchant_specific_rate_applies_even_under_another_category(self):
        self.assertEqual(self._spend("Selectos").amount, Decimal("7.00"))

    def test_merchant_category_overrides_the_transaction_category(self):
        # Un comercio del rubro supermercado sin tasa propia usa la del rubro (3 %),
        # no la de "Comida" -> restaurantes (que caería en el 1 % base).
        other = Merchant.objects.create(name="Walmart", category_type=self.super, aliases="walmart")
        self.assertEqual(other.category_type, self.super)
        self.assertEqual(self._spend("walmart").amount, Decimal("3.00"))

    def test_no_merchant_uses_the_category(self):
        self.assertEqual(self._spend("pupusas").amount, Decimal("1.00"))


class MerchantApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("ana", "ana@example.com", "pw")
        self.staff = User.objects.create_user("root", "root@example.com", "pw", is_staff=True)
        self.super = CategoryType.objects.create(slug="supermercado", name="Supermercado")
        self.merchant = Merchant.objects.create(
            name="Súper Selectos", category_type=self.super, aliases="selectos"
        )
        bank = Bank.objects.create(name="Banco X")
        product = CardProduct.objects.create(bank=bank, name="T")
        self.program = LoyaltyProgram.objects.create(
            card_product=product, kind="cashback", default_rate=Decimal("0.01")
        )

    def test_any_user_can_read_merchants_with_their_aliases(self):
        self.client.force_authenticate(self.user)
        res = self.client.get("/api/v1/loyalty-merchants/")
        self.assertEqual(res.status_code, 200)
        rows = res.json()["results"] if isinstance(res.json(), dict) else res.json()
        self.assertEqual(rows[0]["aliases"], ["Súper Selectos", "selectos"])

    def test_only_staff_can_write(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/v1/loyalty-merchants/", {"name": "X"}).status_code, 403)
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.post("/api/v1/loyalty-merchants/", {"name": "X"}).status_code, 201)

    def test_rate_needs_a_rubro_or_a_merchant_not_both(self):
        self.client.force_authenticate(self.staff)
        base = {"program": str(self.program.pk), "rate": "0.05"}
        url = "/api/v1/loyalty-category-rates/"
        self.assertEqual(self.client.post(url, base).status_code, 400)
        both = {**base, "category_type": str(self.super.pk), "merchant": str(self.merchant.pk)}
        self.assertEqual(self.client.post(url, both).status_code, 400)
        ok = self.client.post(url, {**base, "merchant": str(self.merchant.pk)})
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json()["merchant"], str(self.merchant.pk))
        self.assertIsNone(ok.json()["category_type"])


class CatalogMerchantDataTests(APITestCase):
    def test_every_merchant_rate_names_a_known_merchant_and_rubros_exist(self):
        merchants = {name for name, _, _ in catalog.MERCHANTS}
        rubros = {slug for slug, _ in catalog.CATEGORY_TYPES}
        for _, slug, _ in catalog.MERCHANTS:
            self.assertTrue(slug is None or slug in rubros, slug)
        self.assertLessEqual(set(catalog.CATEGORY_TO_RUBRO.values()), rubros)
        for bank in catalog.CATALOG:
            for item in bank["products"]:
                for program in item["programs"]:
                    for rate in program["merchants"]:
                        self.assertIn(rate[0], merchants, item["name"])

    def test_no_alias_belongs_to_two_merchants(self):
        seen = {}
        for name, _, aliases in catalog.MERCHANTS:
            for alias in [name, *aliases]:
                key = normalize_text(alias)
                # Repetir el nombre del propio comercio como alias es redundante, no un choque.
                self.assertEqual(seen.setdefault(key, name), name, f"{alias!r}: {name} y {seen[key]}")


class MapCategoriesCommandTests(APITestCase):
    def setUp(self):
        call_command("seed_loyalty_catalog", stdout=StringIO())
        self.ws = Workspace.objects.create(name="W")

    def _cat(self, name, **kw):
        return Category.objects.create(workspace=self.ws, name=name, type=Category.TYPE_EXPENSE, **kw)

    def _run(self, *args):
        out = StringIO()
        call_command("map_categories_to_rubros", *args, stdout=out)
        return out.getvalue()

    def test_maps_by_name_ignoring_case_and_accents_and_leaves_the_rest(self):
        gas, edu, ropa = self._cat("gasolina"), self._cat("Educación"), self._cat("Ropa")
        self._run()
        for cat in (gas, edu, ropa):
            cat.refresh_from_db()
        self.assertEqual(gas.category_type.slug, "gasolina")
        self.assertEqual(edu.category_type.slug, "educacion")
        self.assertIsNone(ropa.category_type)

    def test_never_overwrites_a_rubro_chosen_by_hand(self):
        cines = CategoryType.objects.get(slug="cines")
        cat = self._cat("Restaurantes", category_type=cines)
        self._run()
        cat.refresh_from_db()
        self.assertEqual(cat.category_type, cines)

    def test_dry_run_saves_nothing(self):
        cat = self._cat("Gasolina")
        self.assertIn("SIMULACIÓN", self._run("--dry-run"))
        cat.refresh_from_db()
        self.assertIsNone(cat.category_type)

    def test_fails_clearly_when_rubros_are_missing(self):
        CategoryType.objects.all().delete()
        with self.assertRaisesMessage(CommandError, "seed_loyalty_catalog"):
            self._run()

    def test_recompute_generates_what_existing_expenses_would_have_earned(self):
        product = CardProduct.objects.get(name="Tarjeta Dorada Visa")
        card = Wallet.objects.create(
            workspace=self.ws, name="Dorada", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=product,
        )
        gas = self._cat("Gasolina")
        # Miércoles: 2 puntos por dólar en gasolina (Agrícola), 1 de base.
        txn = Transaction.objects.create(
            wallet=card, category=gas, amount=Decimal("50.00"), date="2026-09-23"
        )
        # Sin rubro sólo gana la tasa base.
        self.assertEqual(LoyaltyEarning.objects.get(transaction=txn).points, Decimal("50.00"))
        self.assertIn("1 gasto(s) recalculado(s)", self._run("--recompute"))
        # Ya con rubro gasolina: el bono del miércoles.
        self.assertEqual(LoyaltyEarning.objects.get(transaction=txn).points, Decimal("100.00"))


class RecomputeAndDefaultMappingTests(APITestCase):
    def setUp(self):
        call_command("seed_loyalty_catalog", stdout=StringIO())

    def test_new_workspaces_get_their_default_categories_mapped(self):
        from apps.transactions.services import seed_default_categories

        ws = Workspace.objects.create(name="Nuevo")
        seed_default_categories(ws)
        by_name = {c.name: c.category_type for c in Category.objects.filter(workspace=ws)}
        self.assertEqual(by_name["Gasolina"].slug, "gasolina")
        self.assertEqual(by_name["Comida"].slug, "restaurantes")
        self.assertEqual(by_name["Gimnasio"].slug, "gimnasio-deporte")
        self.assertEqual(by_name["Teléfono"].slug, "telefonia-internet")
        self.assertIsNone(by_name["Ropa"])

    def test_seeding_categories_without_the_loyalty_catalog_does_nothing_odd(self):
        from apps.transactions.services import seed_default_categories

        CategoryType.objects.all().delete()
        ws = Workspace.objects.create(name="Sin catálogo")
        seed_default_categories(ws)
        self.assertFalse(Category.objects.filter(workspace=ws, category_type__isnull=False).exists())

    def test_recompute_registers_what_was_spent_before_the_catalog_existed(self):
        ws = Workspace.objects.create(name="W")
        product = CardProduct.objects.get(name="Tarjeta Dorada Visa")
        card = Wallet.objects.create(
            workspace=ws, name="Dorada", kind=Wallet.KIND_CREDIT,
            credit_limit=Decimal("3000"), billing_cycle_day=15, card_product=product,
        )
        cat = Category.objects.create(workspace=ws, name="Ropa", type=Category.TYPE_EXPENSE)
        txn = Transaction.objects.create(
            wallet=card, category=cat, amount=Decimal("40.00"), date="2026-09-22"
        )
        LoyaltyEarning.objects.filter(transaction=txn).delete()  # como si nunca se hubiera calculado
        out = StringIO()
        call_command("recompute_loyalty_earnings", "--dry-run", stdout=out)
        self.assertIn("SIMULACIÓN", out.getvalue())
        self.assertFalse(LoyaltyEarning.objects.filter(transaction=txn).exists())
        call_command("recompute_loyalty_earnings", stdout=StringIO())
        self.assertEqual(LoyaltyEarning.objects.get(transaction=txn).points, Decimal("40.00"))
