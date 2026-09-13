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
recurrentes, cuotas a plazo, o carteras hijas) -- para ese caso hace falta
mover esa actividad a mano primero (o pedir que se automatice, no lo hace
este comando a propósito: mover plata de una cartera a otra sin que el
usuario vea exactamente qué se movió es el tipo de cosa que conviene
revisar antes de ejecutar, no después)."""
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

        blockers = []
        if Transaction.objects.filter(Q(wallet=origen) | Q(to_wallet=origen)).exists():
            blockers.append("tiene transacciones")
        if RecurringExpense.objects.filter(Q(wallet=origen) | Q(to_wallet=origen)).exists():
            blockers.append("tiene gastos recurrentes")
        if InstallmentPurchase.objects.filter(wallet=origen).exists():
            blockers.append("tiene compras a plazo")
        if origen.children.exists():
            blockers.append("tiene carteras hijas propias")
        if blockers:
            raise CommandError(
                f"'{origen.name}' {', '.join(blockers)} -- este comando solo fusiona carteras "
                "vacías. Mové esa actividad a mano a la cartera destino antes de fusionar."
            )

        with db_transaction.atomic():
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
