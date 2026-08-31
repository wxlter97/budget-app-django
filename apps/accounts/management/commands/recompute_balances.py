from django.core.management.base import BaseCommand

from apps.accounts.models import Account
from apps.accounts.services import recompute_account_balance


class Command(BaseCommand):
    help = "Recalcula Account.current_balance (opening_balance + Σ transacciones vivas)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            help="UUID de un workspace para limitar el recálculo a sus cuentas.",
        )

    def handle(self, *args, **options):
        accounts = Account.all_objects.all()
        if options.get("workspace"):
            accounts = accounts.filter(workspace_id=options["workspace"])

        changed = 0
        for account in accounts:
            before = account.current_balance
            after = recompute_account_balance(account)
            if before != after:
                changed += 1
                self.stdout.write(f"  {account}: {before} -> {after}")

        self.stdout.write(
            self.style.SUCCESS(f"{accounts.count()} cuenta(s) revisadas, {changed} ajustada(s).")
        )
