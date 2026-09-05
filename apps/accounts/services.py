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
import calendar
import math
from collections import defaultdict
from datetime import date as date_cls
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Case, DecimalField, F, Q, Sum, When
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


# ---------------------------------------------------------------------------
# Estado de cuenta de tarjeta de crédito ("cuánto debo a esta fecha", Fase 3
# del roadmap): cuánto hay que transferir para estar al día con el corte más
# reciente, sin depender de que el usuario abra la app justo el día del corte.
# ---------------------------------------------------------------------------


def _clamped_date(year: int, month: int, day: int) -> date_cls:
    """``day`` puede no existir en ``month`` (p. ej. corte el 31 en febrero):
    se recorta al último día real del mes, como hacen los bancos."""
    last_day = calendar.monthrange(year, month)[1]
    return date_cls(year, month, min(day, last_day))


def _cutoff_on_or_before(billing_cycle_day: int, on: date_cls) -> date_cls:
    """Última fecha de corte (día ``billing_cycle_day`` de cada mes) que ya
    pasó, en o antes de ``on``."""
    cutoff = _clamped_date(on.year, on.month, billing_cycle_day)
    if cutoff > on:
        prev = on.replace(day=1) - relativedelta(days=1)
        cutoff = _clamped_date(prev.year, prev.month, billing_cycle_day)
    return cutoff


def _next_cutoff(billing_cycle_day: int, cutoff_date: date_cls) -> date_cls:
    nxt = cutoff_date + relativedelta(months=1)
    return _clamped_date(nxt.year, nxt.month, billing_cycle_day)


def _payment_due_date(wallet: Wallet, cutoff_date: date_cls):
    """Fecha límite de pago correspondiente a un corte: si el día de pago cae
    después del día de corte dentro del mismo mes, es ese mismo mes; si no,
    es al mes siguiente (el caso típico: corte el 3, pago el 20)."""
    if not wallet.payment_due_day:
        return None
    target = cutoff_date
    if wallet.payment_due_day <= wallet.billing_cycle_day:
        target = cutoff_date + relativedelta(months=1)
    return _clamped_date(target.year, target.month, wallet.payment_due_day)


def _statement_components(wallet, until_date):
    """(gastado, abonado, cuotas vencidas) de ``wallet`` acumulado hasta
    ``until_date`` inclusive. Ver `credit_card_statement` para el criterio."""
    from apps.transactions.models import InstallmentPurchase, Transaction

    spent = (
        Transaction.objects.filter(
            wallet=wallet, type=Transaction.TYPE_EXPENSE, date__lte=until_date
        )
        .exclude(source=Transaction.SOURCE_INSTALLMENT)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )
    # Rarísimo pero posible: esta tarjeta financia una compra a plazo AJENA
    # (es `payment_wallet` de otra cartera) -- esas cuotas salen de acá como
    # transferencia, y sí son gasto real de esta tarjeta.
    spent += (
        Transaction.objects.filter(
            wallet=wallet,
            type=Transaction.TYPE_TRANSFER,
            source=Transaction.SOURCE_INSTALLMENT,
            date__lte=until_date,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )

    paid = (
        Transaction.objects.filter(
            to_wallet=wallet, type=Transaction.TYPE_TRANSFER, date__lte=until_date
        )
        .exclude(source=Transaction.SOURCE_INSTALLMENT)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0")
    )

    installments_due = Decimal("0")
    lines = []
    for purchase in InstallmentPurchase.objects.filter(wallet=wallet).select_related(
        "category"
    ):
        n_due = 0
        for n in range(1, purchase.installments_total + 1):
            cuota_date = purchase.start_date + relativedelta(months=n - 1)
            if cuota_date > until_date:
                break
            n_due = n
        if n_due:
            amount_due = purchase.installment_amount * n_due
            installments_due += amount_due
            lines.append(
                {
                    "id": purchase.id,
                    "description": purchase.description,
                    "installments_due": n_due,
                    "installments_total": purchase.installments_total,
                    "amount_due": amount_due,
                }
            )

    return spent, paid, installments_due, lines


def credit_card_statement(wallet, as_of=None):
    """Cuánto hay que pagarle a esta tarjeta para estar al día, a la fecha
    ``as_of`` (hoy por defecto). ``None`` si `wallet` no es una tarjeta de
    crédito con fecha de corte configurada (`kind=credit` + `billing_cycle_day`).

    El total (`total_due`) es ACUMULADO desde que existe la tarjeta hasta el
    corte más reciente que ya cerró en o antes de `as_of`: lo que no se pagó
    de un corte anterior sigue apareciendo hasta que se abone (así es como
    funciona una tarjeta real, no se resetea sola cada mes). Se compone de:

    - los gastos normales cargados a la tarjeta (sin contar el cargo total
      inicial de una compra a plazo -- ese cargo baja el disponible, pero lo
      que hay que *pagar* cada mes es solo la cuota, no la compra completa),
    - más las cuotas de compras a plazo que ya vencieron,
    - menos los abonos/pagos reales que ya se hicieron a la tarjeta.

    También incluye la actividad del período abierto (desde el corte hasta
    `as_of`, aún no vencida) como referencia de cuánto se lleva acumulado
    para el próximo corte.
    """
    if wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day:
        return None

    as_of = as_of or timezone.localdate()
    cutoff_date = _cutoff_on_or_before(wallet.billing_cycle_day, as_of)
    next_cutoff_date = _next_cutoff(wallet.billing_cycle_day, cutoff_date)
    payment_due_date = _payment_due_date(wallet, cutoff_date)

    spent, paid, installments_due, lines = _statement_components(wallet, cutoff_date)
    total_due = spent - paid + installments_due

    spent_open, paid_open, installments_open, _ = _statement_components(wallet, as_of)
    current_period_spent = (spent_open - spent) + (installments_open - installments_due)
    current_period_paid = paid_open - paid

    return {
        "cutoff_date": cutoff_date,
        "next_cutoff_date": next_cutoff_date,
        "payment_due_date": payment_due_date,
        "spent": spent,
        "paid": paid,
        "installments_due": installments_due,
        "total_due": total_due,
        "current_period_spent": current_period_spent,
        "current_period_paid": current_period_paid,
        "installment_lines": lines,
    }


def credit_card_statements_summary(workspace, user, as_of=None):
    """`credit_card_statement` de cada tarjeta de crédito visible del
    workspace (con fecha de corte configurada), para el listado de Herramientas."""
    wallets = (
        Wallet.objects.filter(workspace=workspace, kind=Wallet.KIND_CREDIT, is_archived=False)
        .exclude(billing_cycle_day__isnull=True)
        .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        .order_by("sort_order", "name")
    )
    results = []
    for w in wallets:
        data = credit_card_statement(w, as_of=as_of)
        if data is None:
            continue
        results.append(
            {
                "wallet_id": w.id,
                "wallet_name": w.name,
                "currency": w.currency,
                "card_last4": w.card_last4,
                **data,
            }
        )
    return results
