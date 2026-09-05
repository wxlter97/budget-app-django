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
import math
from collections import defaultdict
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Case, DecimalField, F, Sum, When
from django.db.models.functions import TruncMonth
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


def goal_projection(wallet, months=6):
    """Proyección de una meta de ahorro: "a este ritmo la alcanzás en N
    meses" (Fase 2 del roadmap). ``None`` si `wallet` no es una meta de
    ahorro (`purpose=savings` con `goal_amount`).

    El "ritmo" es el promedio de aporte neto mensual observado en los
    últimos `months` meses de movimientos reales de la cartera (o menos,
    si es más nueva que eso) -- no lo que el usuario dijo que iba a
    aportar (`monthly_contribution`); esa cifra sólo se usa de respaldo
    cuando todavía no hay ningún historial de movimientos.
    """
    if wallet.purpose != Wallet.PURPOSE_SAVINGS or not wallet.goal_amount:
        return None

    remaining = wallet.goal_amount - wallet.current_balance
    if remaining <= 0:
        return {
            "remaining": Decimal("0"),
            "monthly_rate": None,
            "months_to_goal": 0,
            "projected_date": None,
            "on_track": True,
        }

    from apps.transactions.models import Transaction

    until = timezone.localdate().replace(day=1)
    since = until - relativedelta(months=months - 1)
    first_active = max(since, wallet.created_at.date().replace(day=1))

    out_rows = (
        Transaction.objects.filter(wallet=wallet, date__gte=first_active)
        .annotate(period=TruncMonth("date"))
        .values("period")
        .annotate(
            net=Sum(
                Case(
                    When(type=Transaction.TYPE_INCOME, then=F("amount")),
                    default=-F("amount"),
                    output_field=_MONEY,
                )
            )
        )
    )
    in_rows = (
        Transaction.objects.filter(
            to_wallet=wallet, type=Transaction.TYPE_TRANSFER, date__gte=first_active
        )
        .annotate(period=TruncMonth("date"))
        .values("period")
        .annotate(net=Sum("amount", output_field=_MONEY))
    )

    totals = defaultdict(lambda: Decimal("0"))
    for row in out_rows:
        totals[row["period"]] += row["net"] or Decimal("0")
    for row in in_rows:
        totals[row["period"]] += row["net"] or Decimal("0")

    n_periods = max(
        1, (until.year - first_active.year) * 12 + (until.month - first_active.month) + 1
    )
    total_net = sum(totals.values(), Decimal("0"))
    avg_rate = total_net / n_periods

    # Sin ritmo observado (todavía sin movimientos, o neto negativo/nulo):
    # el aporte mensual planeado es lo único con lo que proyectar.
    rate = avg_rate if avg_rate > 0 else (wallet.monthly_contribution or Decimal("0"))
    if rate <= 0:
        return {
            "remaining": remaining,
            "monthly_rate": rate,
            "months_to_goal": None,
            "projected_date": None,
            "on_track": False,
        }

    months_to_goal = math.ceil(remaining / rate)
    projected_date = until + relativedelta(months=months_to_goal)
    on_track = wallet.goal_date is None or projected_date <= wallet.goal_date

    return {
        "remaining": remaining,
        "monthly_rate": rate,
        "months_to_goal": months_to_goal,
        "projected_date": projected_date,
        "on_track": on_track,
    }
