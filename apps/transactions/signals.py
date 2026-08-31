"""Sincroniza `Account.current_balance` ante cambios en Transaction.

Cubre alta, edición (de monto, de categoría income<->expense, o de cuenta)
y borrado — incluido el soft delete, que llega como un ``save`` con
``is_deleted=True``.
"""
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.accounts.services import apply_balance_delta, transaction_effect

from .models import Transaction


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
    new_effect = transaction_effect(instance)

    if prev is None:
        apply_balance_delta(instance.account_id, new_effect)
        return

    old_effect = transaction_effect(prev)
    if prev.account_id != instance.account_id:
        apply_balance_delta(prev.account_id, -old_effect)
        apply_balance_delta(instance.account_id, new_effect)
    else:
        apply_balance_delta(instance.account_id, new_effect - old_effect)


@receiver(post_delete, sender=Transaction)
def _sync_balance_on_delete(sender, instance, **kwargs):
    apply_balance_delta(instance.account_id, -transaction_effect(instance))
