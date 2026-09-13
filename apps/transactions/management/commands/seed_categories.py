"""Crea la estructura de categorías por defecto (grupos → categorías) en es.

Desde que ``WorkspaceViewSet.perform_create`` llama a ``seed_default_categories``
en cada workspace nuevo (ver ``apps.transactions.services``), este comando
sólo hace falta para aplicarlo a mano a workspaces que ya existían de antes
de ese cambio y quedaron en blanco.

    manage.py seed_categories                     # todos los workspaces
    manage.py seed_categories --workspace <UUID>  # solo ese
    manage.py seed_categories --only-empty        # solo los que no tienen ninguna
"""
from django.core.management.base import BaseCommand, CommandError

from apps.transactions.models import Category
from apps.transactions.services import seed_default_categories
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    help = "Crea grupos + categorías por defecto (es) para uno o todos los workspaces."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", help="UUID de un workspace para limitar el seed.")
        parser.add_argument(
            "--only-empty",
            action="store_true",
            help="Saltar los workspaces que ya tienen al menos una categoría.",
        )

    def handle(self, *args, **options):
        workspaces = Workspace.objects.all()
        if options.get("workspace"):
            workspaces = workspaces.filter(id=options["workspace"])
            if not workspaces.exists():
                raise CommandError(f"No existe el workspace {options['workspace']}.")

        total = 0
        for ws in workspaces:
            if options.get("only_empty") and Category.objects.filter(workspace=ws).exists():
                self.stdout.write(f"  {ws}: ya tiene categorías, se salta.")
                continue
            created = seed_default_categories(ws)
            self.stdout.write(f"  {ws}: {created} categoría(s) nueva(s).")
            total += created

        self.stdout.write(
            self.style.SUCCESS(
                f"{workspaces.count()} workspace(s), {total} categoría(s) creada(s)."
            )
        )
