"""
Activa los bancos cuyo parser ya existe en `bank_parsers/` pero todavía no tiene
un `BankEmailSchema` que lo conecte (ver `services._match_schema` / `ingest_inbound_email`:
sin esta fila, un correo de ese banco nunca llega a su parser, aunque el parser en sí
ya esté escrito y probado).

`bank_name` DEBE dar, vía `slugify()`, la misma clave con la que el parser se registró
(`@register("...")`) -- por eso está fijo acá, no es sólo un nombre bonito.

Create-only (como el catálogo de lealtad): si la fila ya existe, no la toca -- para no
pisar un `sender_pattern` afinado a mano tras ver fallar un correo real. `--actualizar`
la fuerza a los valores de acá.

    python manage.py seed_bank_schemas --dry-run
    python manage.py seed_bank_schemas
"""
from django.core.management.base import BaseCommand

from apps.email_import.models import BankEmailSchema

# bank_name -> sender_pattern (regex buscado en el header From completo, ver
# services._match_schema). Sin patrón todavía = no se siembra (ver el aviso al final).
BANKS: dict[str, str | None] = {
    # info@baccredomatic.com -- parser: bank_parsers/bac_credomatic.py
    "Banco de América Central": r"@baccredomatic\.com",
    # notificaciones@bancocuscatlan.com -- parser: bank_parsers/banco_cuscatlan.py
    "Banco Cuscatlán": r"@bancocuscatlan\.com",
    # no-reply@simaninternet.net -- parser: bank_parsers/siman.py
    "Siman": r"@simaninternet\.net",
}


class Command(BaseCommand):
    help = "Crea (o actualiza) los BankEmailSchema de los bancos con parser ya escrito."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No guarda nada; sólo informa.")
        parser.add_argument(
            "--actualizar", action="store_true",
            help="Pisa el sender_pattern existente con el de acá (por defecto, sólo crea lo que falta).",
        )

    def handle(self, *args, **options):
        missing = [name for name, pattern in BANKS.items() if not pattern]
        if missing:
            self.stdout.write(self.style.WARNING(
                "Sin sender_pattern todavía, no se siembran: " + ", ".join(missing)
            ))

        created, updated, skipped = 0, 0, 0
        for name, pattern in BANKS.items():
            if not pattern:
                continue
            existing = BankEmailSchema.all_objects.filter(bank_name=name).first()
            if existing is None:
                created += 1
                if not options["dry_run"]:
                    BankEmailSchema.objects.create(
                        bank_name=name, sender_pattern=pattern, is_active=True
                    )
            elif options["actualizar"]:
                updated += 1
                if not options["dry_run"]:
                    existing.sender_pattern = pattern
                    existing.is_active = True
                    existing.is_deleted = False
                    existing.save()
            else:
                skipped += 1

        prefix = "SIMULACIÓN (no se guardó nada). " if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{created} creado(s), {updated} actualizado(s), {skipped} existente(s) sin tocar."
        ))
