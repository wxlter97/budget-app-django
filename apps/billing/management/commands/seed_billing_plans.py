"""
Carga (o actualiza) los planes Free/Pro con los límites, features y precios
acordados. Idempotente -- correr de nuevo actualiza en vez de duplicar.

    python manage.py seed_billing_plans
"""
from django.core.management.base import BaseCommand

from apps.billing.models import Plan, PlanPrice


class Command(BaseCommand):
    help = "Crea/actualiza los planes Free y Pro con sus límites, features y precios por defecto."

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
            (pro, PlanPrice.BILLING_MONTHLY, 199),
            (pro, PlanPrice.BILLING_ANNUAL, 1999),
            (pro, PlanPrice.BILLING_LIFETIME, 1999),
        ]
        for plan, period, cents in prices:
            PlanPrice.objects.update_or_create(
                plan=plan, billing_period=period, currency="USD",
                defaults={"amount_cents": cents, "is_active": True},
            )

        self.stdout.write(self.style.SUCCESS(
            f"Listo: '{free.code}' (default) y '{pro.code}' con {len(prices)} precio(s)."
        ))
