from django.core.management import call_command
from django.test import TestCase

from apps.email_import.bank_parsers.registry import get_parser
from apps.email_import.management.commands.seed_bank_schemas import BANKS
from apps.email_import.models import BankEmailSchema


class SeedBankSchemasTests(TestCase):
    def test_creates_the_three_banks_with_a_registered_parser(self):
        call_command("seed_bank_schemas")
        self.assertEqual(BankEmailSchema.objects.count(), 3)
        for name in BANKS:
            schema = BankEmailSchema.objects.get(bank_name=name)
            self.assertTrue(schema.is_active)

    def test_each_bank_name_slugifies_to_a_key_with_a_real_parser(self):
        """El `bank_name` sembrado tiene que dar, vía slugify(), la misma clave con la
        que el parser se registró (`@register(...)`) -- si no, `ingest_inbound_email`
        encuentra el schema pero nunca su parser (ver `services._match_schema` y
        `get_parser(slugify(schema.bank_name))`)."""
        from django.utils.text import slugify

        for name in BANKS:
            self.assertIsNotNone(
                get_parser(slugify(name)), f"sin parser registrado para {name!r}"
            )

    def test_dry_run_creates_nothing(self):
        call_command("seed_bank_schemas", "--dry-run")
        self.assertFalse(BankEmailSchema.objects.exists())

    def test_running_twice_does_not_duplicate(self):
        call_command("seed_bank_schemas")
        call_command("seed_bank_schemas")
        self.assertEqual(BankEmailSchema.objects.count(), 3)

    def test_a_hand_tuned_sender_pattern_is_not_overwritten_without_actualizar(self):
        call_command("seed_bank_schemas")
        schema = BankEmailSchema.objects.get(bank_name="Siman")
        schema.sender_pattern = r"@algo-mas-especifico\.simaninternet\.net"
        schema.save()
        call_command("seed_bank_schemas")
        schema.refresh_from_db()
        self.assertEqual(schema.sender_pattern, r"@algo-mas-especifico\.simaninternet\.net")

    def test_actualizar_overwrites_and_revives_a_deleted_row(self):
        call_command("seed_bank_schemas")
        schema = BankEmailSchema.objects.get(bank_name="Siman")
        schema.sender_pattern = "algo-viejo"
        schema.is_deleted = True
        schema.save()
        call_command("seed_bank_schemas", "--actualizar")
        schema.refresh_from_db()
        self.assertEqual(schema.sender_pattern, BANKS["Siman"])
        self.assertFalse(schema.is_deleted)

    def test_the_real_from_headers_match_their_pattern(self):
        """Los remitentes reales que dio el usuario (22-sep-2026), tal como llegarían en
        el header From completo (con nombre de display), no sólo la dirección pelada."""
        import re

        call_command("seed_bank_schemas")
        cases = {
            "Banco de América Central": "BAC Credomatic <info@baccredomatic.com>",
            "Banco Cuscatlán": "Banco Cuscatlán <notificaciones@bancocuscatlan.com>",
            "Siman": "SIMAN <no-reply@simaninternet.net>",
        }
        for bank_name, sender in cases.items():
            schema = BankEmailSchema.objects.get(bank_name=bank_name)
            self.assertIsNotNone(
                re.search(schema.sender_pattern, sender, re.IGNORECASE),
                f"{schema.sender_pattern!r} no matchea {sender!r}",
            )

    def test_the_pattern_does_not_match_an_unrelated_sender(self):
        import re

        call_command("seed_bank_schemas")
        schema = BankEmailSchema.objects.get(bank_name="Banco de América Central")
        self.assertIsNone(re.search(schema.sender_pattern, "iCloud <noreply@email.apple.com>", re.IGNORECASE))
