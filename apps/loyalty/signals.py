"""Sincroniza los `LoyaltyEarning` de puntos y cashback con `Transaction`.

Se recalculan enteros (borra + recrea) en cada save de la transacción, en vez
de a delta -- a esta escala no importa el costo, y así cualquier cambio
(monto, categoría, cartera) queda reflejado sin casos especiales. Cubre el
soft-delete igual que `transactions.signals` (llega como un save con
`is_deleted=True`) y el borrado físico.

El descuento NO se toca acá: lo registra el propio cliente al crear/editar la
transacción (ver `apps.transactions.api.TransactionSerializer`), porque
necesita el monto de ANTES del descuento -- algo que esta señal, que sólo ve
la transacción ya guardada, no puede reconstruir.
"""
from decimal import Decimal

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.transactions.models import Transaction

from .models import LoyaltyEarning, LoyaltyProgram

_AUTO_KINDS = (LoyaltyProgram.KIND_POINTS, LoyaltyProgram.KIND_CASHBACK)


def _clear(instance: Transaction) -> None:
    LoyaltyEarning.objects.filter(transaction=instance, kind__in=_AUTO_KINDS).delete()


def _recompute(instance: Transaction) -> None:
    _clear(instance)
    if instance.is_deleted or instance.type != Transaction.TYPE_EXPENSE:
        return

    card_product_id = instance.wallet.card_product_id
    category_type = instance.category.category_type if instance.category_id else None
    if not card_product_id or category_type is None:
        return

    programs = LoyaltyProgram.objects.filter(
        card_product_id=card_product_id, is_active=True, kind__in=_AUTO_KINDS
    )
    for program in programs:
        rate = program.rate_for(category_type)
        if not rate:
            continue
        earned = (instance.amount * rate).quantize(Decimal("0.01"))
        if program.kind == LoyaltyProgram.KIND_POINTS:
            LoyaltyEarning.objects.create(
                workspace_id=instance.wallet.workspace_id,
                transaction=instance,
                program=program,
                kind=LoyaltyProgram.KIND_POINTS,
                points=earned,
            )
        else:
            LoyaltyEarning.objects.create(
                workspace_id=instance.wallet.workspace_id,
                transaction=instance,
                program=program,
                kind=LoyaltyProgram.KIND_CASHBACK,
                amount=earned,
            )


@receiver(post_save, sender=Transaction)
def sync_loyalty_earnings(sender, instance, **kwargs):
    _recompute(instance)


@receiver(post_delete, sender=Transaction)
def remove_loyalty_earnings(sender, instance, **kwargs):
    _clear(instance)
