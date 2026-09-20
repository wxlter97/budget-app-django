"""Carga el catálogo de lealtad de los bancos de El Salvador (`apps/loyalty/catalog.py`).

**El admin es la fuente de verdad.** Por defecto este comando sólo CREA lo que
falta (bancos, tarjetas, programas, tasas, comercios) y **nunca modifica, revive ni
borra** lo que ya existe: si alguien corrigió una tasa, renombró una tarjeta o
eliminó un comercio desde el admin, correr el comando otra vez no lo deshace. Es
seguro correrlo las veces que haga falta, p. ej. para recibir las tarjetas nuevas que
se agreguen a `catalog.py`.

`--actualizar` es la excepción, deliberada y explícita: pisa lo existente con lo que
dice `catalog.py` (valores, nombres, compra mínima…), revive lo que se había borrado y
aplica los `RENAMES`. Sirve para la primera carga sobre datos que se habían escrito a
mano con errores, o para restaurar el catálogo entero. Pisa correcciones hechas en
el admin: usarlo sólo sabiéndolo.

    manage.py seed_loyalty_catalog --dry-run               # qué crearía; no guarda nada
    manage.py seed_loyalty_catalog                         # crea lo que falta
    manage.py seed_loyalty_catalog --actualizar --dry-run  # qué pisaría
    manage.py seed_loyalty_catalog --actualizar
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


def _upsert(model, lookup: dict, defaults: dict, counts: dict, label: str, overwrite: bool):
    """Crea la fila si no existe. Si ya existe (aunque esté borrada), la deja como
    está, salvo con `overwrite`, que la actualiza y la revive (las filas borradas
    con soft delete chocarían con las constraints de unicidad si se recrearan)."""
    obj = model.all_objects.filter(**lookup).first()
    if obj is None:
        counts[label + " creados"] = counts.get(label + " creados", 0) + 1
        return model.all_objects.create(**lookup, **defaults)
    if not overwrite:
        counts[label + " existentes (sin tocar)"] = counts.get(label + " existentes (sin tocar)", 0) + 1
        return obj
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
        parser.add_argument(
            "--actualizar", action="store_true",
            help="Pisa lo existente con lo de catalog.py (y aplica los renombres). Ver la "
            "docstring: pisa correcciones hechas en el admin.",
        )

    def handle(self, *args, **options):
        self.overwrite = options["actualizar"]
        counts: dict[str, int] = {}
        with transaction.atomic():
            types = {}
            for slug, name in catalog.CATEGORY_TYPES:
                types[slug] = _upsert(
                    CategoryType, {"slug": slug}, {"name": name}, counts, "rubros", self.overwrite
                )

            merchants = {}
            for name, slug, aliases in catalog.MERCHANTS:
                merchants[name] = _upsert(
                    Merchant, {"name": name},
                    {"category_type": types[slug] if slug else None, "aliases": "\n".join(aliases)},
                    counts, "comercios", self.overwrite,
                )

            if self.overwrite:
                for bank_name, old, new in catalog.RENAMES:
                    product = CardProduct.all_objects.filter(bank__name=bank_name, name=old).first()
                    clash = CardProduct.all_objects.filter(bank__name=bank_name, name=new).exists()
                    if product and not clash:
                        product.name = new
                        product.save()
                        counts["productos renombrados"] = counts.get("productos renombrados", 0) + 1
            # Sin --actualizar no se renombra, pero tampoco se crea un duplicado de una tarjeta
            # que ya existe con su nombre anterior (las carteras están asignadas a esa).
            legacy = {(b, new): old for b, old, new in catalog.RENAMES}

            for entry in catalog.CATALOG:
                bank = _upsert(Bank, {"name": entry["bank"]}, {}, counts, "bancos", self.overwrite)
                for item in entry["products"]:
                    name = item["name"]
                    old = legacy.get((entry["bank"], name))
                    if (
                        not self.overwrite and old
                        and not CardProduct.all_objects.filter(bank=bank, name=name).exists()
                        and CardProduct.all_objects.filter(bank=bank, name=old).exists()
                    ):
                        name = old
                    product = _upsert(
                        CardProduct, {"bank": bank, "name": name},
                        {"network": item["network"]}, counts, "productos", self.overwrite,
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
            "min_amount": None if spec["min_amount"] is None else _dec(spec["min_amount"]),
        }
        if program is None:
            program = LoyaltyProgram.all_objects.create(
                card_product=product, kind=spec["kind"], **defaults
            )
            counts["programas creados"] = counts.get("programas creados", 0) + 1
        elif self.overwrite:
            changed = program.is_deleted
            for field, value in defaults.items():
                # No pisar un valor de canje o un mínimo puestos a mano si el catálogo no trae uno.
                if field in ("point_value", "min_amount") and value is None:
                    continue
                if getattr(program, field) != value:
                    setattr(program, field, value)
                    changed = True
            if changed:
                program.is_deleted = False
                program.save()
                counts["programas actualizados"] = counts.get("programas actualizados", 0) + 1
        else:
            counts["programas existentes (sin tocar)"] = counts.get("programas existentes (sin tocar)", 0) + 1

        for rate in spec["rates"]:
            slug, value = rate[0], rate[1]
            weekday = rate[2] if len(rate) > 2 else None
            autopay = rate[3] if len(rate) > 3 else False
            _upsert(
                LoyaltyCategoryRate,
                {"program": program, "category_type": types[slug], "merchant": None,
                 "weekday": weekday, "requires_autopay": autopay},
                {"rate": _dec(value)}, counts, "tasas", self.overwrite,
            )
        for rate in spec["merchants"]:
            name, value = rate[0], rate[1]
            weekday = rate[2] if len(rate) > 2 else None
            _upsert(
                LoyaltyCategoryRate,
                {"program": program, "category_type": None, "merchant": merchants[name],
                 "weekday": weekday, "requires_autopay": False},
                {"rate": _dec(value)}, counts, "tasas de comercio", self.overwrite,
            )
