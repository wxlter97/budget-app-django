"""Sincroniza `Wallet.current_balance` ante cambios en Transaction.

Cubre alta, edición (de monto, tipo, cartera origen/destino) y borrado
—incluido el soft delete, que llega como un ``save`` con ``is_deleted=True``.
Cada transacción puede afectar a más de una cartera (transferencias), así que
se trabaja con un dict ``{wallet_id: delta}``.
"""
import logging

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.accounts.services import apply_balance_delta, balance_deltas

from .models import Transaction

logger = logging.getLogger(__name__)


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


@receiver(post_delete, sender=Transaction)
def _delete_receipt_file(sender, instance, **kwargs):
    """Borra del storage el recibo de una transacción borrada de verdad.

    Django no borra archivos al borrar filas (desde 1.3): sin esto, cada
    borrado físico deja la foto huérfana en el bucket, pagándose para siempre
    sin que nada la pueda volver a mostrar. Lo notan sobre todo los borrados
    en cascada —`wipe_workspace_data` al vaciar o al restaurar un backup borra
    todas las transacciones de una— donde no hay ninguna vista de por medio
    que se acuerde de limpiar.

    Ojo con lo que NO cubre: el borrado desde la app es *soft* (is_deleted),
    y ahí el recibo tiene que quedarse, porque la transacción se puede
    recuperar. Mientras no exista una purga de soft-deleted, ese archivo vive
    lo que viva la fila.

    Si el storage falla no se propaga el error: el usuario pidió borrar una
    transacción, no subir un archivo, y hacer fallar el borrado (que además
    revierte la transacción de base entera) por un blob es peor que dejar el
    blob. Queda en los logs.
    """
    if not instance.receipt:
        return
    try:
        instance.receipt.delete(save=False)
    except Exception:  # noqa: BLE001 - ver docstring
        logger.warning(
            "No se pudo borrar el recibo %s de la transacción %s",
            instance.receipt.name,
            instance.pk,
            exc_info=True,
        )
