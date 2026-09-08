"""Sincroniza `Wallet.current_balance` ante cambios en Transaction.

Cubre alta, edición (de monto, tipo, cartera origen/destino) y borrado
—incluido el soft delete, que llega como un ``save`` con ``is_deleted=True``.
Cada transacción puede afectar a más de una cartera (transferencias), así que
se trabaja con un dict ``{wallet_id: delta}``.
"""
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.accounts.services import apply_balance_delta, balance_deltas

from .models import Transaction


def _apply_diff(old: dict, new: dict) -> None:
    for wallet_id in set(old) | set(new):
        apply_balance_delta(wallet_id, new.get(wallet_id, 0) - old.get(wallet_id, 0))


@receiver(pre_save, sender=Transaction)
def _snapshot_previous_state(sender, instance, **kwargs):
    if instance._state.adding:
        instance._balance_prev = None
    else:
        instance._balance_prev = (
            Transaction.all_objects.select_related("category")
            .filter(pk=instance.pk)
            .first()
        )


@receiver(post_save, sender=Transaction)
def _sync_balance_on_save(sender, instance, created, **kwargs):
    prev = getattr(instance, "_balance_prev", None)
    _apply_diff(balance_deltas(prev), balance_deltas(instance))


def _clear_split_group_if_alone(split_group) -> None:
    """Si a una transacción dividida le queda una sola parte viva (se
    borraron o rechazaron las demás), ya no es "dividida" -- se le limpia
    el grupo para que vuelva a comportarse como una transacción normal."""
    if not split_group:
        return
    remaining = Transaction.objects.filter(split_group=split_group)
    if remaining.count() == 1:
        remaining.update(split_group=None)


@receiver(post_save, sender=Transaction)
def _clear_split_group_on_soft_delete(sender, instance, **kwargs):
    # El soft-delete llega como un save() con is_deleted=True (ver arriba);
    # ahí es donde una parte "desaparece" para todo lo demás.
    if instance.is_deleted:
        _clear_split_group_if_alone(instance.split_group)


@receiver(post_delete, sender=Transaction)
def _sync_balance_on_delete(sender, instance, **kwargs):
    _apply_diff(balance_deltas(instance), {})
    _clear_split_group_if_alone(instance.split_group)
