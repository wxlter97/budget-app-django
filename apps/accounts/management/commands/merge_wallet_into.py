"""Fusiona dos carteras que en realidad son la MISMA cuenta -- típicamente
el error de haber creado una cartera aparte para una tarjeta adicional
(titular + adicional comparten saldo/límite/estado de cuenta, ver docstring
de `WalletCard`) en vez de agregarle el número de plástico a la que ya
existía.

    python manage.py merge_wallet_into <origen> <destino> [--workspace <ws>]

<origen> y <destino> aceptan un UUID o el nombre de la cartera (exacto,
sin importar mayúsculas). <origen> se borra (soft-delete) al final; su
`card_last4` y sus `extra_cards`, si tenía, quedan como tarjetas
adicionales de <destino> (o como su `card_last4` principal, si <destino>
todavía no tenía uno).

Se niega a fusionar si <origen> tiene actividad propia (transacciones,
recurrentes, cuotas a plazo, o carteras hijas) -- salvo que se pase
--move-activity, en cuyo caso esa actividad se reasigna a <destino> (UPDATE
de la FK `wallet`/`to_wallet`, no se recrean filas) antes del soft-delete.
Es un flag aparte -- no el comportamiento por defecto -- porque mover plata
de una cartera a otra sin que el usuario vea exactamente qué se movió es el
tipo de cosa que conviene revisar antes de ejecutar, no después: usá
--dry-run junto con --move-activity para ver los conteos sin escribir nada.

--move-activity se niega, igual que sin el flag, si encuentra una
transacción o gasto recurrente que ya conecta <origen> y <destino> entre sí
(por ejemplo una transferencia de una tarjeta a la otra) -- fusionarlas
crearía una fila con wallet == to_wallet, que no tiene sentido. Esos casos
hay que resolverlos a mano."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Q

from apps.accounts.models import Wallet, WalletCard
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    help = "Fusiona una cartera vacía (típicamente una tarjeta adicional mal separada) en otra."

    def add_arguments(self, parser):
        parser.add_argument("origen", help="UUID o nombre de la cartera a fusionar (se borra).")
        parser.add_argument("destino", help="UUID o nombre de la cartera que se queda.")
        parser.add_argument("--workspace", help="UUID o nombre del workspace, si hay más de uno.")
        parser.add_argument(
            "--move-activity",
            action="store_true",
            help="Si <origen> tiene transacciones/recurrentes/cuotas/hijas, reasignarlas a "
            "<destino> en vez de negarse.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Con --move-activity: mostrar qué se movería, sin escribir nada.",
        )

    def _resolve_workspace(self, raw):
        if raw:
            ws = Workspace.objects.filter(id=raw).first() if _looks_like_uuid(raw) else None
            ws = ws or Workspace.objects.filter(name__iexact=raw).first()
            if ws is None:
                raise CommandError(f"No se encontró el workspace {raw!r}.")
            return ws
        workspaces = list(Workspace.objects.all()[:2])
        if len(workspaces) != 1:
            raise CommandError(
                "Hay más de un workspace (o ninguno) -- indicá --workspace <uuid o nombre>."
            )
        return workspaces[0]

    def _resolve_wallet(self, raw, workspace, label):
        qs = Wallet.objects.filter(workspace=workspace)
        wallet = qs.filter(id=raw).first() if _looks_like_uuid(raw) else None
        if wallet is None:
            matches = list(qs.filter(name__iexact=raw))
            if len(matches) > 1:
                raise CommandError(
                    f"Hay {len(matches)} carteras llamadas {raw!r} en este workspace -- usá el UUID."
                )
            wallet = matches[0] if matches else None
        if wallet is None:
            raise CommandError(f"No se encontró la cartera {label} ({raw!r}).")
        return wallet

    def handle(self, *args, **options):
        from apps.transactions.models import InstallmentPurchase, RecurringExpense, Transaction

        workspace = self._resolve_workspace(options.get("workspace"))
        origen = self._resolve_wallet(options["origen"], workspace, "origen")
        destino = self._resolve_wallet(options["destino"], workspace, "destino")

        if origen.id == destino.id:
            raise CommandError("Origen y destino son la misma cartera.")

        move_activity = options["move_activity"]
        dry_run = options["dry_run"]

        # Una transacción/recurrente que ya conecta origen y destino (p.ej.
        # un pago de una tarjeta con la otra) se volvería wallet == to_wallet
        # si la fusionamos -- eso no se puede reasignar automáticamente.
        crossed = []
        if Transaction.objects.filter(
            Q(wallet=origen, to_wallet=destino) | Q(wallet=destino, to_wallet=origen)
        ).exists():
            crossed.append("transacciones")
        if RecurringExpense.objects.filter(
            Q(wallet=origen, to_wallet=destino) | Q(wallet=destino, to_wallet=origen)
        ).exists():
            crossed.append("gastos recurrentes")
        if crossed:
            raise CommandError(
                f"Hay {', '.join(crossed)} entre '{origen.name}' y '{destino.name}' -- "
                "fusionarlas dejaría una fila con la misma cartera como origen y destino. "
                "Resolvé eso a mano antes de fusionar."
            )

        tx_qs = Transaction.objects.filter(Q(wallet=origen) | Q(to_wallet=origen))
        rec_qs = RecurringExpense.objects.filter(Q(wallet=origen) | Q(to_wallet=origen))
        inst_qs = InstallmentPurchase.objects.filter(wallet=origen)
        # excluye a destino por si el orden de los argumentos viene invertido
        # (destino ya es hija de origen): no tiene sentido auto-parentarla.
        children_qs = origen.children.exclude(pk=destino.id)

        blockers = []
        if tx_qs.exists():
            blockers.append("tiene transacciones")
        if rec_qs.exists():
            blockers.append("tiene gastos recurrentes")
        if inst_qs.exists():
            blockers.append("tiene compras a plazo")
        if children_qs.exists():
            blockers.append("tiene carteras hijas propias")

        if blockers and not move_activity:
            raise CommandError(
                f"'{origen.name}' {', '.join(blockers)} -- este comando solo fusiona carteras "
                "vacías salvo que uses --move-activity. Mové esa actividad a mano a la cartera "
                "destino antes de fusionar, o volvé a correr con --move-activity."
            )

        if move_activity:
            counts = {
                "transacciones": tx_qs.count(),
                "gastos recurrentes": rec_qs.count(),
                "compras a plazo": inst_qs.count(),
                "carteras hijas": children_qs.count(),
            }
            self.stdout.write(
                "Se reasignarían de '{}' a '{}': {}.".format(
                    origen.name,
                    destino.name,
                    ", ".join(f"{v} {k}" for k, v in counts.items()) or "nada",
                )
            )
            if dry_run:
                self.stdout.write(self.style.WARNING("--dry-run: no se escribió nada."))
                return

        with db_transaction.atomic():
            if move_activity:
                Transaction.objects.filter(wallet=origen).update(wallet=destino)
                Transaction.objects.filter(to_wallet=origen).update(to_wallet=destino)
                RecurringExpense.objects.filter(wallet=origen).update(wallet=destino)
                RecurringExpense.objects.filter(to_wallet=origen).update(to_wallet=destino)
                inst_qs.update(wallet=destino)
                children_qs.update(parent=destino)

            existing_last4s = {destino.card_last4} | set(
                destino.extra_cards.values_list("last4", flat=True)
            )
            moved = []

            if origen.card_last4 and origen.card_last4 not in existing_last4s:
                if not destino.card_last4:
                    destino.card_last4 = origen.card_last4
                    destino.save(update_fields=["card_last4", "updated_at"])
                else:
                    WalletCard.objects.create(
                        wallet=destino, last4=origen.card_last4, label=origen.name
                    )
                moved.append(origen.card_last4)
                existing_last4s.add(origen.card_last4)

            for card in origen.extra_cards.all():
                if card.last4 in existing_last4s:
                    continue
                WalletCard.objects.create(wallet=destino, last4=card.last4, label=card.label or origen.name)
                moved.append(card.last4)
                existing_last4s.add(card.last4)

            origen.soft_delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"'{origen.name}' fusionada en '{destino.name}'. "
                + (f"Números de tarjeta trasladados: {', '.join(moved)}." if moved else "Sin números de tarjeta que trasladar.")
            )
        )


def _looks_like_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False
