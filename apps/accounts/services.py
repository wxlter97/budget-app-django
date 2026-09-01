"""Mantenimiento del saldo (`current_balance`) de las carteras.

Convención de signo:
- ``income``   -> suma al saldo de ``wallet``
- ``expense``  -> resta del saldo de ``wallet``
- ``transfer`` -> resta de ``wallet`` y suma a ``to_wallet``
``amount`` siempre es positivo.

`current_balance` es el saldo PROPIO de la cartera (sin hijos): valor cacheado
que se mantiene incrementalmente vía signals sobre Transaction (ver
apps/transactions/signals.py) y se puede reconstruir con
`recompute_wallet_balance` / `manage.py recompute_balances`.
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, When
from django.utils import timezone

from .models import Wallet

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def balance_deltas(txn) -> dict:
    """Efecto (con signo) de una transacción *viva* sobre el saldo de cada
    cartera implicada: ``{wallet_id: Decimal}``."""
    from apps.transactions.models import Transaction

    if txn is None or txn.is_deleted:
        return {}

    # `amount` puede ser aún un str si la instancia no ha vuelto de la BD.
    amount = Decimal(txn.amount)

    if txn.type == Transaction.TYPE_INCOME:
        return {txn.wallet_id: amount}
    if txn.type == Transaction.TYPE_EXPENSE:
        return {txn.wallet_id: -amount}
    if txn.type == Transaction.TYPE_TRANSFER:
        deltas = {txn.wallet_id: -amount}
        if txn.to_wallet_id:
            deltas[txn.to_wallet_id] = deltas.get(txn.to_wallet_id, Decimal("0")) + amount
        return deltas
    return {}


def apply_balance_delta(wallet_id, delta) -> None:
    if not delta:
        return
    Wallet.objects.filter(pk=wallet_id).update(
        current_balance=F("current_balance") + delta,
        updated_at=timezone.now(),
    )


def recompute_wallet_balance(wallet) -> Decimal:
    """Recalcula `current_balance` (propio) desde cero: opening_balance + Σ
    transacciones vivas (income +, expense -, transfer saliente -, entrante +)."""
    from apps.transactions.models import Transaction

    out = Transaction.objects.filter(wallet=wallet).aggregate(
        total=Sum(
            Case(
                When(type=Transaction.TYPE_INCOME, then=F("amount")),
                default=-F("amount"),
                output_field=_MONEY,
            )
        )
    )["total"] or Decimal("0")

    incoming = Transaction.objects.filter(
        to_wallet=wallet, type=Transaction.TYPE_TRANSFER
    ).aggregate(total=Sum("amount", output_field=_MONEY))["total"] or Decimal("0")

    wallet.current_balance = wallet.opening_balance + out + incoming
    wallet.save(update_fields=["current_balance", "updated_at"])
    return wallet.current_balance
