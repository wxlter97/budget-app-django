"""Asigna a cada categoría de los workspaces su rubro de lealtad (`CategoryType`).

Sin rubro, una categoría no genera puntos ni cashback (ver `apps.loyalty.signals`).
El mapeo por nombre está en `apps.loyalty.catalog.CATEGORY_TO_RUBRO`. Sólo llena las
categorías que **no tienen rubro todavía**: nunca pisa uno que se eligió a mano.
Los rubros tienen que existir antes (`seed_loyalty_catalog`).

    manage.py map_categories_to_rubros --dry-run
    manage.py map_categories_to_rubros
    manage.py map_categories_to_rubros --workspace <UUID>

Después de mapear, las transacciones **ya existentes** no recalculan solas lo que
habrían ganado (la señal corre al guardar). Para eso: `--recompute`.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.loyalty import catalog
from apps.loyalty.models import CategoryType
from apps.loyalty.services import map_categories_to_rubros, normalize_text, recompute_earnings
from apps.transactions.models import Category, Transaction


class Command(BaseCommand):
    help = "Asigna el rubro de lealtad a las categorías sin rubro, según su nombre."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No guarda nada; sólo informa.")
        parser.add_argument("--workspace", help="UUID de un workspace para limitar el mapeo.")
        parser.add_argument(
            "--recompute", action="store_true",
            help="Recalcula lo ganado por los gastos ya existentes de las categorías mapeadas.",
        )

    def handle(self, *args, **options):
        rubros = {t.slug: t for t in CategoryType.objects.all()}
        missing = sorted(set(catalog.CATEGORY_TO_RUBRO.values()) - set(rubros))
        if missing:
            raise CommandError(
                f"Faltan rubros ({', '.join(missing)}): corré antes seed_loyalty_catalog."
            )

        categories = Category.objects.all()
        if options["workspace"]:
            categories = categories.filter(workspace_id=options["workspace"])

        mapped, skipped = [], {}
        with transaction.atomic():
            mapped = map_categories_to_rubros(categories.select_related("workspace"))
            for category in mapped:
                self.stdout.write(
                    f"  {category.workspace.name}: {category.name} -> {category.category_type.slug}"
                )
            for category in categories.filter(category_type__isnull=True):
                if normalize_text(category.name) not in catalog.CATEGORY_TO_RUBRO:
                    skipped[category.name] = skipped.get(category.name, 0) + 1

            recomputed = 0
            if options["recompute"] and mapped:
                recomputed = recompute_earnings(Transaction.objects.filter(
                    category__in=mapped, type=Transaction.TYPE_EXPENSE,
                    wallet__card_product__isnull=False,
                ))

            if options["dry_run"]:
                transaction.set_rollback(True)

        prefix = "SIMULACIÓN (no se guardó nada). " if options["dry_run"] else ""
        if skipped:
            self.stdout.write("  Sin rubro (a propósito): " + ", ".join(sorted(skipped)))
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{len(mapped)} categoría(s) mapeada(s)"
            + (f", {recomputed} gasto(s) recalculado(s)." if options["recompute"] else ".")
        ))
