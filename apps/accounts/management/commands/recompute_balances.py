from django.core.management.base import BaseCommand

from apps.accounts.models import Wallet
from apps.accounts.services import recompute_wallet_balance


class Command(BaseCommand):
    help = "Recalcula Wallet.current_balance (opening_balance + Σ transacciones vivas)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            help="UUID de un workspace para limitar el recálculo a sus carteras.",
        )

    def handle(self, *args, **options):
        wallets = Wallet.all_objects.all()
        if options.get("workspace"):
            wallets = wallets.filter(workspace_id=options["workspace"])

        changed = 0
        for wallet in wallets:
            before = wallet.current_balance
            after = recompute_wallet_balance(wallet)
            if before != after:
                changed += 1
                self.stdout.write(f"  {wallet}: {before} -> {after}")

        self.stdout.write(
            self.style.SUCCESS(f"{wallets.count()} cartera(s) revisadas, {changed} ajustada(s).")
        )
