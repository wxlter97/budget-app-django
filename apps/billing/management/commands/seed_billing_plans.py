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
                description="El loop diario: anotar y ver tus gastos, en solitario.",
                is_default=True,
                max_workspaces_owned=1,
                # 1 = sólo el owner (sin invitados) y 0 recurrentes activos:
                # el gratis dejó de ser "un Pro más chico" y pasó a ser
                # deliberadamente restrictivo (22-sep-2026, decisión de
                # negocio -- es la única palanca de monetización que hay,
                # sin publicidad ni venta de datos de por medio). Ver
                # ECONOMIA-POR-PLAN.md.
                max_members_per_workspace=1,
                max_active_recurring=0,
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
                    # Fuera del gratis por completo desde el 22-sep-2026 (ver
                    # arriba): cada una es una función que antes estaba
                    # disponible sin pagar y ahora es la razón para pasarse a
                    # Plus. `has_feature_for_workspace`/`require_feature_for_workspace`
                    # (apps/billing/services.py) son quienes la hacen cumplir;
                    # `calendar` y `transaction_duplicate` no tienen chequeo de
                    # backend propio (no hay un endpoint separado que gatear
                    # sin romper la lista normal de transacciones) y se gatean
                    # sólo del lado del frontend.
                    "calendar": False,
                    "notifications": False,
                    "wallet_split": False,
                    "transaction_duplicate": False,
                    "refunds": False,
                    "split_categories": False,
                    "split_people": False,
                    "installments": False,
                    "statements": False,
                    "net_worth": False,
                    # Cuotas de IA (ver apps/ai/quotas.py y el backlog de
                    # funciones nuevas). En Free la IA es una muestra: alcanza
                    # para probarla y no para que salga cara.
                    "ai_receipts_per_month": 3,
                    "ai_parses_per_month": 10,
                    "ai_chats_per_month": 0,
                },
            ),
        )
        plus, _ = Plan.objects.update_or_create(
            code="plus",
            defaults=dict(
                name="Plus",
                description="Todo lo que el gratis deja afuera: miembros, recurrentes, "
                             "calendario, avisos, patrimonio y dividir gastos.",
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
                    "calendar": True,
                    "notifications": True,
                    "wallet_split": True,
                    "transaction_duplicate": True,
                    "refunds": True,
                    "split_categories": True,
                    "split_people": True,
                    "installments": True,
                    "statements": True,
                    "net_worth": True,
                    # Techo de costo de IA ~$0.085/mes contra $0.99 de precio.
                    "ai_receipts_per_month": 30,
                    "ai_parses_per_month": 50,
                    "ai_chats_per_month": 20,
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
                    "calendar": True,
                    "notifications": True,
                    "wallet_split": True,
                    "transaction_duplicate": True,
                    "refunds": True,
                    "split_categories": True,
                    "split_people": True,
                    "installments": True,
                    "statements": True,
                    "net_worth": True,
                    # Techo ~$0.36/mes contra $1.99. El lifetime de $19.99 usa
                    # estas mismas cuotas, no IA ilimitada: un usuario
                    # intensivo sin tope serían ~14 años de consumo sólo para
                    # empatar ese pago único.
                    "ai_receipts_per_month": 100,
                    "ai_parses_per_month": 200,
                    "ai_chats_per_month": 100,
                },
            ),
        )

        prices = [
            (plus, PlanPrice.BILLING_MONTHLY, 99, True),
            (plus, PlanPrice.BILLING_ANNUAL, 999, True),
            (pro, PlanPrice.BILLING_MONTHLY, 199, True),
            # Antes 1999 (19.99): descuento más agresivo en el anual para
            # empujar la conversión desde mensual (~4 meses gratis en vez de ~2).
            (pro, PlanPrice.BILLING_ANNUAL, 1499, True),
            # Desactivado (22-sep-2026, decisión de negocio): un pago único sin
            # cobro recurrente detrás es la exposición real del negocio (ver
            # ECONOMIA-POR-PLAN.md, tabla C) -- un usuario que lo usa al techo de
            # Pro se paga solo en 1.6-2.4 años. Se deja la fila (no se borra) por
            # si alguien ya lo compró; `is_active=False` sólo le quita el botón a
            # los nuevos.
            (pro, PlanPrice.BILLING_LIFETIME, 1999, False),
        ]
        for plan, period, cents, active in prices:
            PlanPrice.objects.update_or_create(
                plan=plan, billing_period=period, currency="USD",
                defaults={"amount_cents": cents, "is_active": active},
            )

        self.stdout.write(self.style.SUCCESS(
            f"Listo: '{free.code}' (default), '{plus.code}' y '{pro.code}' con {len(prices)} precio(s)."
        ))
