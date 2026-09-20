"""`seed_loyalty_catalog` y los datos de `apps.loyalty.catalog`."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.loyalty import catalog
from apps.loyalty.models import Bank, CardProduct, CategoryType, LoyaltyCategoryRate, LoyaltyProgram


def _seed(*args):
    out = StringIO()
    call_command("seed_loyalty_catalog", *args, stdout=out)
    return out.getvalue()


class CatalogDataTests(TestCase):
    """Los datos a mano se equivocan de formas tontas; esto las atrapa."""

    def test_every_rate_points_to_a_known_category_type(self):
        known = {slug for slug, _ in catalog.CATEGORY_TYPES}
        for bank in catalog.CATALOG:
            for item in bank["products"]:
                for program in item["programs"]:
                    for rate in program["rates"]:
                        self.assertIn(rate[0], known, f'{item["name"]}: rubro {rate[0]}')

    def test_no_duplicated_products_or_rates(self):
        for bank in catalog.CATALOG:
            names = [p["name"] for p in bank["products"]]
            self.assertEqual(len(names), len(set(names)), bank["bank"])
            for item in bank["products"]:
                kinds = [p["kind"] for p in item["programs"]]
                self.assertEqual(len(kinds), len(set(kinds)), item["name"])
                for program in item["programs"]:
                    keys = [(r[0], r[2] if len(r) > 2 else None, len(r) > 3 and r[3]) for r in program["rates"]]
                    self.assertEqual(len(keys), len(set(keys)), item["name"])

    def test_percent_programs_use_fractions_and_weekdays_are_valid(self):
        for bank in catalog.CATALOG:
            for item in bank["products"]:
                for program in item["programs"]:
                    values = [program["default"]] + [r[1] for r in program["rates"]]
                    if program["kind"] != "points":
                        self.assertTrue(all(0 <= v <= 1 for v in values), item["name"])
                    for r in program["rates"]:
                        if len(r) > 2 and r[2] is not None:
                            self.assertIn(r[2], range(7))


class SeedCommandTests(TestCase):
    def test_creates_the_catalog(self):
        _seed()
        dorada = CardProduct.objects.get(bank__name="Banco Agrícola", name="Tarjeta Dorada Visa")
        program = dorada.programs.get()
        self.assertEqual(str(program.default_rate), "1.0000")
        self.assertEqual(str(program.point_value), "0.0050")
        monday = LoyaltyCategoryRate.objects.get(
            program=program, category_type__slug="supermercado", weekday=0
        )
        self.assertEqual(str(monday.rate), "2.0000")

    def test_is_idempotent(self):
        _seed()
        counts = (
            Bank.objects.count(), CardProduct.objects.count(),
            LoyaltyProgram.objects.count(), LoyaltyCategoryRate.objects.count(),
            CategoryType.objects.count(),
        )
        _seed()
        self.assertEqual(counts, (
            Bank.objects.count(), CardProduct.objects.count(),
            LoyaltyProgram.objects.count(), LoyaltyCategoryRate.objects.count(),
            CategoryType.objects.count(),
        ))

    def test_dry_run_saves_nothing(self):
        out = _seed("--dry-run")
        self.assertIn("SIMULACIÓN", out)
        self.assertEqual(CardProduct.objects.count(), 0)

    def test_renames_an_existing_product_keeping_its_id_and_fixes_its_program(self):
        bank = Bank.objects.create(name="Banco de América Central")
        old = CardProduct.objects.create(bank=bank, name="CASHBACK BLUE", network="other")
        LoyaltyProgram.objects.create(
            card_product=old, kind="cashback", name="AMEX BLUE", default_rate="0.01"
        )
        old_econ = CardProduct.objects.create(bank=bank, name="Economía", network="visa")
        LoyaltyProgram.objects.create(
            card_product=old_econ, kind="cashback", name="SUPERMERCADOS", default_rate="0.05"
        )
        _seed("--actualizar")
        old.refresh_from_db()
        self.assertEqual((old.name, old.network), ("American Express Blue", "amex"))
        # El programa existente se reutiliza, no se duplica.
        self.assertEqual(old.programs.count(), 1)
        # El 5 % dejó de ser la tasa de todo: quedó como tasa de Supermercado.
        econ = CardProduct.objects.get(pk=old_econ.pk)
        self.assertEqual(econ.name, "EconoMía Clásica")
        program = econ.programs.get()
        self.assertEqual(str(program.default_rate), "0.0100")
        self.assertEqual(str(program.rate_for(CategoryType.objects.get(slug="supermercado"))), "0.0500")

    def test_does_not_overwrite_a_hand_set_point_value_when_catalog_has_none(self):
        _seed()
        program = LoyaltyProgram.objects.get(
            card_product__name="Mastercard Clásica", card_product__bank__name="Banco de América Central"
        )
        program.point_value = "0.0100"
        program.save()
        _seed()
        program.refresh_from_db()
        self.assertEqual(str(program.point_value), "0.0100")

    def test_revives_a_soft_deleted_rate_instead_of_colliding(self):
        _seed()
        rate = LoyaltyCategoryRate.objects.filter(weekday=0).first()
        rate.soft_delete()
        _seed("--actualizar")
        rate.refresh_from_db()
        self.assertFalse(rate.is_deleted)


class CuscatlanUnoTests(TestCase):
    """El descuento de UNO es sólo en gasolineras UNO y tiendas Pronto, no en todo:
    antes estaba cargado como 6 % de tasa base."""

    def test_the_discount_only_applies_to_uno_stations_and_pronto(self):
        from apps.loyalty.models import Merchant
        from apps.loyalty.services import match_merchant

        _seed()
        program = LoyaltyProgram.objects.get(
            card_product__name="UNO", card_product__bank__name="Banco Cuscatlán", kind="discount"
        )
        gasolina = CategoryType.objects.get(slug="gasolina")
        uno = match_merchant("gasolina uno metrocentro")
        pronto = Merchant.objects.get(name="Tiendas Pronto")
        self.assertEqual(uno.name, "Gasolineras UNO")
        self.assertEqual(str(program.rate_for(gasolina, None, uno)), "0.0600")
        self.assertEqual(str(program.rate_for(None, None, pronto)), "0.0600")
        # Otra gasolinera, o la misma sin decir cuál, no recibe el descuento.
        self.assertEqual(str(program.rate_for(gasolina)), "0.0000")
        self.assertIsNone(match_merchant("gasolina shell"))

    def test_it_replaces_the_old_6_percent_base_rate_of_an_existing_uno(self):
        bank = Bank.objects.create(name="Banco Cuscatlán")
        product = CardProduct.objects.create(bank=bank, name="UNO", network="visa")
        old = LoyaltyProgram.objects.create(
            card_product=product, kind="discount", name="Descuento UNO", default_rate="0.06"
        )
        _seed("--actualizar")
        old.refresh_from_db()
        self.assertEqual(str(old.default_rate), "0.0000")
        self.assertEqual(product.programs.filter(kind="discount").count(), 1)


class AdminIsTheSourceOfTruthTests(TestCase):
    """Por defecto el seed sólo crea lo que falta: nada de lo que se cambió desde el
    admin se deshace al volver a correrlo."""

    def setUp(self):
        _seed()

    def test_an_edited_rate_program_and_product_survive_a_second_run(self):
        program = LoyaltyProgram.objects.get(
            card_product__name="Tarjeta Dorada Visa", card_product__bank__name="Banco Agrícola"
        )
        program.default_rate = "3"
        program.name = "Nombre del admin"
        program.min_amount = "25"
        program.save()
        rate = LoyaltyCategoryRate.objects.get(program=program, category_type__slug="supermercado")
        rate.rate = "9"
        rate.save()
        product = program.card_product
        product.network = "mastercard"
        product.save()

        out = _seed()
        program.refresh_from_db(); rate.refresh_from_db(); product.refresh_from_db()
        self.assertEqual((str(program.default_rate), program.name, str(program.min_amount)),
                         ("3.0000", "Nombre del admin", "25.00"))
        self.assertEqual(str(rate.rate), "9.0000")
        self.assertEqual(product.network, "mastercard")
        self.assertIn("sin tocar", out)

    def test_deleted_things_stay_deleted(self):
        from apps.loyalty.models import Merchant

        rate = LoyaltyCategoryRate.objects.filter(weekday=0).first()
        rate.soft_delete()
        merchant = Merchant.objects.get(name="Walmart")
        merchant.soft_delete()
        product = CardProduct.objects.get(name="Tarjeta Clásica Visa")
        product.soft_delete()
        counts = (CardProduct.all_objects.count(), Merchant.all_objects.count())

        _seed()
        rate.refresh_from_db(); merchant.refresh_from_db(); product.refresh_from_db()
        self.assertTrue(rate.is_deleted and merchant.is_deleted and product.is_deleted)
        # y tampoco se volvieron a crear con otro id
        self.assertEqual(counts, (CardProduct.all_objects.count(), Merchant.all_objects.count()))

    def test_new_entries_added_to_the_catalog_later_are_created(self):
        from apps.loyalty.models import Merchant

        Merchant.all_objects.filter(name="Walmart").delete()
        CardProduct.all_objects.filter(name="Tarjeta Clásica Visa").delete()
        _seed()
        self.assertTrue(Merchant.objects.filter(name="Walmart").exists())
        self.assertTrue(CardProduct.objects.filter(name="Tarjeta Clásica Visa").exists())

    def test_a_card_with_its_old_name_is_not_duplicated_nor_renamed(self):
        LoyaltyProgram.objects.all().delete()
        CardProduct.all_objects.filter(name="Tarjeta Dorada Visa").delete()
        bank = Bank.objects.get(name="Banco Agrícola")
        old = CardProduct.objects.create(bank=bank, name="Tarjeta Oro", network="visa")
        _seed()
        self.assertFalse(CardProduct.objects.filter(name="Tarjeta Dorada Visa").exists())
        old.refresh_from_db()
        self.assertEqual(old.name, "Tarjeta Oro")
        # pero sí se le cargan los programas que le faltaban
        self.assertEqual(old.programs.count(), 1)

    def test_actualizar_overwrites_on_purpose_and_dry_run_does_not_save(self):
        program = LoyaltyProgram.objects.get(
            card_product__name="Tarjeta Dorada Visa", card_product__bank__name="Banco Agrícola"
        )
        program.default_rate = "3"
        program.save()
        self.assertIn("SIMULACIÓN", _seed("--actualizar", "--dry-run"))
        program.refresh_from_db()
        self.assertEqual(str(program.default_rate), "3.0000")
        _seed("--actualizar")
        program.refresh_from_db()
        self.assertEqual(str(program.default_rate), "1.0000")
