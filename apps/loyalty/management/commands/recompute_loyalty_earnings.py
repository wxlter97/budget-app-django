"""Recalcula los puntos y el cashback de los gastos que ya existían.

La señal de lealtad sólo corre al guardar una transacción: lo que se gastó
**antes** de cargar el catálogo (o antes de que un gasto sin rubro pudiera ganar
la tasa base) no tiene su ganancia registrada. Este comando la calcula con las
reglas actuales para todos los gastos hechos con una tarjeta que tiene producto.
Es repetible (borra y vuelve a crear lo automático de cada gasto) y no toca los
descuentos, que registra el cliente al aplicarlos.

    manage.py recompute_loyalty_earnings --dry-run
    manage.py recompute_loyalty_earnings
    manage.py recompute_loyalty_earnings --workspace <UUID>
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.loyalty.models import LoyaltyEarning, LoyaltyProgram
from apps.loyalty.services import recompute_earnings
from apps.transactions.models import Transaction
from apps.workspaces.models import Workspace

_AUTO = (LoyaltyProgram.KIND_POINTS, LoyaltyProgram.KIND_CASHBACK)


class Command(BaseCommand):
    help = "Recalcula los puntos y el cashback de los gastos hechos con tarjetas con producto."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No guarda nada; sólo informa.")
        parser.add_argument("--workspace", help="UUID de un workspace para limitar el cálculo.")

    def handle(self, *args, **options):
        txns = Transaction.objects.filter(
            type=Transaction.TYPE_EXPENSE, wallet__card_product__isnull=False
        )
        if options["workspace"]:
            if not Workspace.objects.filter(id=options["workspace"]).exists():
                raise CommandError(f"No existe el workspace {options['workspace']}.")
            txns = txns.filter(wallet__workspace_id=options["workspace"])

        before = LoyaltyEarning.objects.filter(kind__in=_AUTO).count()
        with transaction.atomic():
            count = recompute_earnings(txns)
            after = LoyaltyEarning.objects.filter(kind__in=_AUTO).count()
            if options["dry_run"]:
                transaction.set_rollback(True)

        prefix = "SIMULACIÓN (no se guardó nada). " if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{count} gasto(s) recorrido(s): ganancias de puntos y cashback "
            f"{before} -> {after}."
        ))
