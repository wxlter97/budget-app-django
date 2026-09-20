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
from datetime import date
from decimal import Decimal

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.transactions.models import Transaction

from .models import LoyaltyEarning, LoyaltyProgram
from .services import match_merchant

_AUTO_KINDS = (LoyaltyProgram.KIND_POINTS, LoyaltyProgram.KIND_CASHBACK)


def _clear(instance: Transaction) -> None:
    LoyaltyEarning.objects.filter(transaction=instance, kind__in=_AUTO_KINDS).delete()


def _recompute(instance: Transaction) -> None:
    _clear(instance)
    if instance.is_deleted or instance.type != Transaction.TYPE_EXPENSE:
        return

    card_product_id = instance.wallet.card_product_id
    if not card_product_id:
        return
    # Recién creada con `objects.create(date="2026-09-01")` la fecha sigue siendo
    # el texto que se le pasó, no un `date`.
    on = instance.date if isinstance(instance.date, date) else date.fromisoformat(str(instance.date))
    # El comercio reconocido en la descripción manda sobre la categoría: una
    # categoría "Comida" mezcla restaurantes con supermercados.
    # Lo que eligió quien registró el gasto manda sobre lo que se deduce del texto.
    merchant = instance.merchant if instance.merchant_id else match_merchant(instance.description)
    category_type = (merchant.category_type if merchant else None) or (
        instance.category.category_type if instance.category_id else None
    )
    # Sin rubro ni comercio no hay tasa especial que buscar, pero la tasa base del
    # programa igual aplica: "1 punto por dólar" vale para cualquier compra.

    programs = LoyaltyProgram.objects.filter(
        card_product_id=card_product_id, is_active=True, kind__in=_AUTO_KINDS
    ).prefetch_related("category_rates")
    for program in programs:
        # Compra mínima ("cashback a partir de $10"): por debajo, este programa no gana.
        if not program.qualifies(instance.amount):
            continue
        rate = program.rate_for(category_type, on, merchant, instance.is_autopay)
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
