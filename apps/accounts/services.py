"""Mantenimiento del saldo (`current_balance`) de las cuentas.

Convención de signo: una transacción cuya categoría es de tipo ``income``
suma; una de tipo ``expense`` resta. ``amount`` siempre es positivo.

`current_balance` es un valor cacheado: se mantiene incrementalmente vía
signals sobre Transaction (ver apps/transactions/signals.py) y se puede
reconstruir con `recompute_account_balance` / `manage.py recompute_balances`.
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, When
from django.utils import timezone

from .models import Account

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def transaction_effect(txn) -> Decimal:
    """Efecto con signo de una transacción *viva* sobre el saldo de su cuenta."""
    from apps.transactions.models import Category

    if txn is None or txn.is_deleted:
        return Decimal("0")
    sign = 1 if txn.category.type == Category.TYPE_INCOME else -1
    return sign * txn.amount


def apply_balance_delta(account_id, delta) -> None:
    if not delta:
        return
    Account.objects.filter(pk=account_id).update(
        current_balance=F("current_balance") + delta,
        updated_at=timezone.now(),
    )


def recompute_account_balance(account) -> Decimal:
    """Recalcula `current_balance` desde cero: opening_balance + Σ transacciones vivas."""
    from apps.transactions.models import Category, Transaction

    total = Transaction.objects.filter(account=account).aggregate(
        total=Sum(
            Case(
                When(category__type=Category.TYPE_INCOME, then=F("amount")),
                default=-F("amount"),
                output_field=_MONEY,
            )
        )
    )["total"] or Decimal("0")

    account.current_balance = account.opening_balance + total
    account.save(update_fields=["current_balance", "updated_at"])
    return account.current_balance
