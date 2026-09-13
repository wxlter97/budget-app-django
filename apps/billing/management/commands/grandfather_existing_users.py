"""
Corré esto UNA VEZ, justo después de `seed_billing_plans`, la primera vez
que se activa el sistema de planes en un entorno que ya tenía usuarios
reales (no en uno nuevo, ni en tests -- ahí no hay nadie a quien
grandfather-ear).

Por qué hace falta: antes de que existiera `Plan`/`Subscription`, todo el
mundo tenía acceso ilimitado a todo (multi-moneda, respaldo, importación,
etc.). En cuanto corre `seed_billing_plans`, cualquier usuario sin
suscripción cae en el plan Free -- alguien que ya usaba una de esas
funciones activamente perdería el acceso de un día para el otro, no por
haber excedido un límite al crear algo nuevo (eso ya fallaba abierto,
`can_add_member`/`can_own_another_workspace` sólo bloquean crear MÁS), sino
por los feature flags, que son todo-o-nada.

Qué hace: le da a cada usuario que ya existía ANTES del corte una
`Subscription` manual al plan Pro, activa, sin fecha de vencimiento --
mismo mecanismo que un alta manual cualquiera (ver `ManualProvider` y
`Subscription.notes`), no un plan especial nuevo. Usuarios que ya tienen
una suscripción activa (p. ej. alguien de prueba que ya pagó) se saltan,
así que correrlo dos veces no duplica nada.

    python manage.py grandfather_existing_users --dry-run   # ver cuántos, sin tocar nada
    python manage.py grandfather_existing_users             # aplicar de verdad
    python manage.py grandfather_existing_users --before 2026-09-13T00:00:00Z
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.billing.models import PROVIDER_MANUAL, Plan, Subscription
from apps.billing.services import active_subscription_for

User = get_user_model()

NOTE = "Grandfathered: cuenta creada antes de que existiera el plan Free/Pro."


class Command(BaseCommand):
    help = "Da acceso Pro permanente (manual) a los usuarios que ya existían antes del plan Free/Pro."

    def add_arguments(self, parser):
        parser.add_argument(
            "--before",
            help="Sólo usuarios con date_joined antes de esta fecha/hora ISO 8601 "
                 "(default: ahora mismo, es decir, todos los que ya existen).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Sólo cuenta e imprime quiénes se verían afectados, no crea nada.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now()
        if options["before"]:
            parsed = parse_datetime(options["before"])
            if parsed is None:
                raise CommandError("--before debe ser una fecha/hora ISO 8601, p. ej. 2026-09-13T00:00:00Z")
            cutoff = parsed

        try:
            pro = Plan.objects.get(code="pro")
        except Plan.DoesNotExist:
            raise CommandError("No existe el plan 'pro' todavía -- corré seed_billing_plans primero.")

        candidates = User.objects.filter(date_joined__lt=cutoff).order_by("date_joined")
        to_grandfather = [u for u in candidates if active_subscription_for(u) is None]

        if options["dry_run"]:
            self.stdout.write(f"{len(to_grandfather)} de {candidates.count()} usuarios con date_joined "
                               f"< {cutoff.isoformat()} recibirían Pro grandfathered (dry-run, nada creado).")
            for u in to_grandfather[:20]:
                self.stdout.write(f"  - {u.username} ({u.email}), alta {u.date_joined:%Y-%m-%d}")
            if len(to_grandfather) > 20:
                self.stdout.write(f"  ... y {len(to_grandfather) - 20} más.")
            return

        Subscription.objects.bulk_create([
            Subscription(
                user=u, plan=pro, status=Subscription.STATUS_ACTIVE,
                provider=PROVIDER_MANUAL, current_period_end=None, notes=NOTE,
            )
            for u in to_grandfather
        ])
        self.stdout.write(self.style.SUCCESS(
            f"Listo: {len(to_grandfather)} usuario(s) con acceso Pro grandfathered."
        ))
