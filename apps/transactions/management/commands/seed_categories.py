"""Crea la estructura de categorías por defecto (grupos → categorías) en es.

Estilo Buddy: cada grupo (categoría sin `parent`) es un bucket de presupuesto
y contiene categorías asignables. Idempotente: solo agrega lo que falte
(match por workspace + nombre + tipo).

    manage.py seed_categories                     # todos los workspaces
    manage.py seed_categories --workspace <UUID>  # solo ese
    manage.py seed_categories --only-empty        # solo los que no tienen ninguna
"""
from django.core.management.base import BaseCommand, CommandError

from apps.transactions.models import Category
from apps.workspaces.models import Workspace

# grupo: (nombre, icono, color, [ (categoría, icono, color), ... ])
EXPENSE_GROUPS = [
    ("Vivienda", "🏠", "#F59E0B", [
        ("Alquiler / Préstamo", "🏦", "#F59E0B"),
        ("Internet", "📶", "#F59E0B"),
        ("Electricidad", "⚡", "#F59E0B"),
        ("Agua", "💧", "#F59E0B"),
        ("Teléfono", "📱", "#F59E0B"),
        ("Mantenimiento", "🔧", "#F59E0B"),
    ]),
    ("Comida", "🍽", "#3B82F6", [
        ("Comida", "🍽", "#3B82F6"),
        ("Supermercado", "🛒", "#3B82F6"),
        ("Restaurantes", "🍔", "#3B82F6"),
    ]),
    ("Transporte", "🚗", "#8B5CF6", [
        ("Gasolina", "⛽", "#8B5CF6"),
        ("Transporte público", "🚌", "#8B5CF6"),
        ("Parking", "🅿️", "#8B5CF6"),
        ("Costes de vehículo", "🚗", "#8B5CF6"),
    ]),
    ("Estilo de vida", "✨", "#EC4899", [
        ("Suscripciones", "🔁", "#EC4899"),
        ("Entretenimiento", "🎬", "#EC4899"),
        ("Ropa", "👕", "#EC4899"),
        ("Gimnasio", "🏋️", "#EC4899"),
        ("Bienestar", "❤️", "#EC4899"),
        ("Regalos", "🎁", "#EC4899"),
        ("Hobby", "🎨", "#EC4899"),
    ]),
    ("Salud", "🏥", "#EF4444", [
        ("Salud", "🏥", "#EF4444"),
        ("Farmacia", "💊", "#EF4444"),
    ]),
    ("Educación", "📚", "#6366F1", [
        ("Educación", "📚", "#6366F1"),
    ]),
    ("Ahorro", "🐷", "#14B8A6", [
        ("Ahorro", "🐷", "#14B8A6"),
    ]),
    ("Otros", "📦", "#94A3B8", [
        ("Impuestos", "🧾", "#94A3B8"),
        ("Comisiones", "🏛️", "#94A3B8"),
        ("Otros gastos", "💸", "#94A3B8"),
    ]),
]

INCOME_GROUPS = [
    ("Ingresos", "💰", "#22C55E", [
        ("Sueldo", "💼", "#22C55E"),
        ("Freelance", "🧑‍💻", "#22C55E"),
        ("Inversiones", "📈", "#22C55E"),
        ("Reembolsos", "↩️", "#22C55E"),
        ("Regalos", "🎁", "#22C55E"),
        ("Otros ingresos", "💰", "#22C55E"),
    ]),
]


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
            total += self._seed_workspace(ws)

        self.stdout.write(
            self.style.SUCCESS(
                f"{workspaces.count()} workspace(s), {total} categoría(s) creada(s)."
            )
        )

    def _seed_workspace(self, ws) -> int:
        created = 0
        order = 0
        for cat_type, groups in (
            (Category.TYPE_EXPENSE, EXPENSE_GROUPS),
            (Category.TYPE_INCOME, INCOME_GROUPS),
        ):
            for gname, gicon, gcolor, children in groups:
                group, made = Category.objects.get_or_create(
                    workspace=ws, name=gname, type=cat_type, parent=None,
                    defaults={"icon": gicon, "color": gcolor, "sort_order": order},
                )
                order += 1
                created += int(made)
                for cname, cicon, ccolor in children:
                    _, made = Category.objects.get_or_create(
                        workspace=ws, name=cname, type=cat_type,
                        defaults={
                            "icon": cicon, "color": ccolor,
                            "parent": group, "sort_order": order,
                        },
                    )
                    order += 1
                    created += int(made)
        self.stdout.write(f"  {ws}: {created} categoría(s) nueva(s).")
        return created
