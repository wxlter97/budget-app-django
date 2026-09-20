"""Carga el catálogo de lealtad de los bancos de El Salvador (`apps/loyalty/catalog.py`).

Se puede correr las veces que haga falta: lo que ya existe se actualiza y lo que
falta se crea. No borra nada (ni bancos, ni productos, ni tasas que no estén en
el catálogo); los productos renombrados (`RENAMES`) conservan su id, así que las
carteras que ya los tienen asignados no se pierden.

    manage.py seed_loyalty_catalog --dry-run   # muestra qué haría y no guarda nada
    manage.py seed_loyalty_catalog
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.loyalty import catalog
from apps.loyalty.models import (
    Bank,
    CardProduct,
    CategoryType,
    LoyaltyCategoryRate,
    LoyaltyProgram,
    Merchant,
)


def _dec(value) -> Decimal:
    # `str` para no arrastrar el error binario de un float (0.015 -> 0.01499999…).
    return Decimal(str(value))


def _upsert(model, lookup: dict, defaults: dict, counts: dict, label: str):
    """`update_or_create` que además cuenta creados/actualizados y revive filas
    borradas (soft delete): si no, chocarían con las constraints de unicidad."""
    obj = model.all_objects.filter(**lookup).first()
    if obj is None:
        counts[label + " creados"] = counts.get(label + " creados", 0) + 1
        return model.all_objects.create(**lookup, **defaults)
    changed = obj.is_deleted
    for field, value in defaults.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed = True
    if changed:
        obj.is_deleted = False
        obj.save()
        counts[label + " actualizados"] = counts.get(label + " actualizados", 0) + 1
    return obj


class Command(BaseCommand):
    help = "Carga o actualiza el catálogo de lealtad (bancos, tarjetas, programas y tasas)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No guarda nada; sólo informa.")

    def handle(self, *args, **options):
        counts: dict[str, int] = {}
        with transaction.atomic():
            types = {}
            for slug, name in catalog.CATEGORY_TYPES:
                types[slug] = _upsert(CategoryType, {"slug": slug}, {"name": name}, counts, "rubros")

            merchants = {}
            for name, slug, aliases in catalog.MERCHANTS:
                merchants[name] = _upsert(
                    Merchant, {"name": name},
                    {"category_type": types[slug] if slug else None, "aliases": "\n".join(aliases)},
                    counts, "comercios",
                )

            for bank_name, old, new in catalog.RENAMES:
                product = CardProduct.all_objects.filter(bank__name=bank_name, name=old).first()
                clash = CardProduct.all_objects.filter(bank__name=bank_name, name=new).exists()
                if product and not clash:
                    product.name = new
                    product.save()
                    counts["productos renombrados"] = counts.get("productos renombrados", 0) + 1

            for entry in catalog.CATALOG:
                bank = _upsert(Bank, {"name": entry["bank"]}, {}, counts, "bancos")
                for item in entry["products"]:
                    product = _upsert(
                        CardProduct, {"bank": bank, "name": item["name"]},
                        {"network": item["network"]}, counts, "productos",
                    )
                    for spec in item["programs"]:
                        self._program(product, spec, types, merchants, counts)

            if options["dry_run"]:
                transaction.set_rollback(True)

        prefix = "SIMULACIÓN (no se guardó nada). " if options["dry_run"] else ""
        for label in sorted(counts):
            self.stdout.write(f"  {label}: {counts[label]}")
        self.stdout.write(self.style.SUCCESS(f"{prefix}Catálogo de lealtad listo."))

    def _program(self, product, spec, types, merchants, counts):
        program = (
            LoyaltyProgram.all_objects.filter(card_product=product, kind=spec["kind"])
            .order_by("created_at")
            .first()
        )
        defaults = {
            "name": spec["name"],
            "default_rate": _dec(spec["default"]),
            "point_value": None if spec["point_value"] is None else _dec(spec["point_value"]),
            "is_active": spec["active"],
        }
        if program is None:
            program = LoyaltyProgram.all_objects.create(
                card_product=product, kind=spec["kind"], **defaults
            )
            counts["programas creados"] = counts.get("programas creados", 0) + 1
        else:
            changed = program.is_deleted
            for field, value in defaults.items():
                # No pisar un valor de canje puesto a mano si el catálogo no trae uno.
                if field == "point_value" and value is None:
                    continue
                if getattr(program, field) != value:
                    setattr(program, field, value)
                    changed = True
            if changed:
                program.is_deleted = False
                program.save()
                counts["programas actualizados"] = counts.get("programas actualizados", 0) + 1

        for rate in spec["rates"]:
            slug, value = rate[0], rate[1]
            weekday = rate[2] if len(rate) > 2 else None
            _upsert(
                LoyaltyCategoryRate,
                {"program": program, "category_type": types[slug], "merchant": None, "weekday": weekday},
                {"rate": _dec(value)}, counts, "tasas",
            )
        for rate in spec["merchants"]:
            name, value = rate[0], rate[1]
            weekday = rate[2] if len(rate) > 2 else None
            _upsert(
                LoyaltyCategoryRate,
                {"program": program, "category_type": None, "merchant": merchants[name], "weekday": weekday},
                {"rate": _dec(value)}, counts, "tasas de comercio",
            )
