"""
Carga (o actualiza) los planes Free/Plus/Pro con los límites, features y
precios acordados. Idempotente -- correr de nuevo actualiza en vez de
duplicar.

    python manage.py seed_billing_plans
"""
from django.core.management.base import BaseCommand

from apps.billing.models import Plan, PlanPrice


class Command(BaseCommand):
    help = "Crea/actualiza los planes Free, Plus y Pro con sus límites, features y precios por defecto."

    def handle(self, *args, **options):
        free, _ = Plan.objects.update_or_create(
            code="free",
            defaults=dict(
                name="Gratis",
                description="El loop diario: anotar y ver tus gastos.",
                is_default=True,
                max_workspaces_owned=1,
                max_members_per_workspace=2,
                max_active_recurring=5,
                features={
                    "import_email": False,
                    "import_excel": False,
                    "net_worth_history": False,
                    "advanced_reports": False,
                    "export": False,
                    "backup": False,
                    "loyalty": False,
                    "multi_currency": False,
                    "quick_add": False,
                },
            ),
        )
        plus, _ = Plan.objects.update_or_create(
            code="plus",
            defaults=dict(
                name="Plus",
                description="Más lugar para crecer: workspaces y recurrentes extra, "
                             "exportar tus datos, multi-moneda e historial de patrimonio.",
                is_default=False,
                max_workspaces_owned=2,
                max_members_per_workspace=5,
                max_active_recurring=15,
                features={
                    "import_email": False,
                    "import_excel": False,
                    "net_worth_history": True,
                    "advanced_reports": False,
                    "export": True,
                    "backup": False,
                    "loyalty": False,
                    "multi_currency": True,
                    "quick_add": False,
                },
            ),
        )
        pro, _ = Plan.objects.update_or_create(
            code="pro",
            defaults=dict(
                name="Pro",
                description="Workspaces y miembros ilimitados, importación automática, "
                             "reportes avanzados y respaldo con versiones.",
                is_default=False,
                max_workspaces_owned=None,
                max_members_per_workspace=None,
                max_active_recurring=None,
                features={
                    "import_email": True,
                    "import_excel": True,
                    "net_worth_history": True,
                    "advanced_reports": True,
                    "export": True,
                    "backup": True,
                    "loyalty": True,
                    "multi_currency": True,
                    "quick_add": True,
                },
            ),
        )

        prices = [
            (plus, PlanPrice.BILLING_MONTHLY, 99),
            (plus, PlanPrice.BILLING_ANNUAL, 999),
            (pro, PlanPrice.BILLING_MONTHLY, 199),
            # Antes 1999 (19.99): descuento más agresivo en el anual para
            # empujar la conversión desde mensual (~4 meses gratis en vez de ~2).
            (pro, PlanPrice.BILLING_ANNUAL, 1499),
            (pro, PlanPrice.BILLING_LIFETIME, 1999),
        ]
        for plan, period, cents in prices:
            PlanPrice.objects.update_or_create(
                plan=plan, billing_period=period, currency="USD",
                defaults={"amount_cents": cents, "is_active": True},
            )

        self.stdout.write(self.style.SUCCESS(
            f"Listo: '{free.code}' (default), '{plus.code}' y '{pro.code}' con {len(prices)} precio(s)."
        ))
