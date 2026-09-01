"""Mantenimiento del saldo (`current_balance`) de las cuentas.

Convención de signo:
- ``income``   -> suma al saldo de ``account``
- ``expense``  -> resta del saldo de ``account``
- ``transfer`` -> resta de ``account`` y suma a ``to_account``
``amount`` siempre es positivo.

`current_balance` es un valor cacheado: se mantiene incrementalmente vía
signals sobre Transaction (ver apps/transactions/signals.py) y se puede
reconstruir con `recompute_account_balance` / `manage.py recompute_balances`.
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, When
from django.utils import timezone

from .models import Account

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def balance_deltas(txn) -> dict:
    """Efecto (con signo) de una transacción *viva* sobre el saldo de cada
    cuenta implicada: ``{account_id: Decimal}``."""
    from apps.transactions.models import Transaction

    if txn is None or txn.is_deleted:
        return {}

    # `amount` puede ser aún un str si la instancia no ha vuelto de la BD.
    amount = Decimal(txn.amount)

    if txn.type == Transaction.TYPE_INCOME:
        return {txn.account_id: amount}
    if txn.type == Transaction.TYPE_EXPENSE:
        return {txn.account_id: -amount}
    if txn.type == Transaction.TYPE_TRANSFER:
        deltas = {txn.account_id: -amount}
        if txn.to_account_id:
            deltas[txn.to_account_id] = deltas.get(txn.to_account_id, Decimal("0")) + amount
        return deltas
    return {}


def transaction_effect(txn) -> Decimal:
    """Efecto sobre el saldo de ``txn.account`` (compat / conveniencia)."""
    if txn is None:
        return Decimal("0")
    return balance_deltas(txn).get(txn.account_id, Decimal("0"))


def apply_balance_delta(account_id, delta) -> None:
    if not delta:
        return
    Account.objects.filter(pk=account_id).update(
        current_balance=F("current_balance") + delta,
        updated_at=timezone.now(),
    )


def recompute_account_balance(account) -> Decimal:
    """Recalcula `current_balance` desde cero: opening_balance + Σ transacciones vivas
    (income +, expense -, transfer saliente -, transfer entrante +)."""
    from apps.transactions.models import Transaction

    out = Transaction.objects.filter(account=account).aggregate(
        total=Sum(
            Case(
                When(type=Transaction.TYPE_INCOME, then=F("amount")),
                default=-F("amount"),
                output_field=_MONEY,
            )
        )
    )["total"] or Decimal("0")

    incoming = Transaction.objects.filter(
        to_account=account, type=Transaction.TYPE_TRANSFER
    ).aggregate(total=Sum("amount", output_field=_MONEY))["total"] or Decimal("0")

    account.current_balance = account.opening_balance + out + incoming
    account.save(update_fields=["current_balance", "updated_at"])
    return account.current_balance
