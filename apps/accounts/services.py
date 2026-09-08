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


def _months_to_payoff(principal: Decimal, monthly_payment: Decimal, annual_rate_pct: Decimal):
    """Meses para saldar `principal` pagando `monthly_payment` cada mes, con
    interés compuesto mensual derivado de `annual_rate_pct` (tasa anual, %)
    -- fórmula estándar de amortización. Sin esto, un `restante / ritmo`
    simple subestima el plazo de cualquier deuda con interés real.
    ``None`` si el pago no alcanza siquiera a cubrir el interés del mes (la
    deuda nunca bajaría con ese ritmo)."""
    monthly_rate = float(annual_rate_pct) / 100 / 12
    principal_f = float(principal)
    payment_f = float(monthly_payment)
    if monthly_rate <= 0:
        return math.ceil(principal_f / payment_f)
    interest_only = principal_f * monthly_rate
    if payment_f <= interest_only:
        return None
    n = -math.log(1 - (monthly_rate * principal_f) / payment_f) / math.log(1 + monthly_rate)
    return math.ceil(n)


def goal_projection(wallet, months=6):
    """Proyección de "a este ritmo la alcanzás / la saldás en N meses": para
    una meta de ahorro (`purpose=savings`) o para una deuda con monto total
    conocido (`purpose=debt`, `goal_amount` = monto total de la deuda).
    ``None`` si `wallet` no aplica a ninguno de los dos casos.

    El "ritmo" es el promedio de aporte/pago neto mensual observado en los
    últimos `months` meses de movimientos reales de la cartera (o menos,
    si es más nueva que eso) -- no lo que el usuario dijo que iba a
    aportar (`monthly_contribution`); esa cifra sólo se usa de respaldo
    cuando todavía no hay ningún historial de movimientos.

    En una deuda con `interest_rate` configurada, los meses para saldarla
    se calculan con amortización (ver `_months_to_payoff`) en vez del simple
    `restante / ritmo` -- el interés compuesto alarga el plazo real.
    """
    is_savings = wallet.purpose == Wallet.PURPOSE_SAVINGS
    is_debt = wallet.purpose == Wallet.PURPOSE_DEBT
    if not (is_savings or is_debt) or not wallet.goal_amount:
        return None

    remaining = (
        wallet.goal_amount - wallet.current_balance if is_savings else abs(wallet.current_balance)
    )
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

    if is_debt and wallet.interest_rate:
        months_to_goal = _months_to_payoff(remaining, rate, wallet.interest_rate)
        if months_to_goal is None:
            return {
                "remaining": remaining,
                "monthly_rate": rate,
                "months_to_goal": None,
                "projected_date": None,
                "on_track": False,
            }
    else:
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


def _cutoff_on_or_after(billing_cycle_day: int, on: date_cls) -> date_cls:
    """Primera fecha de corte (día ``billing_cycle_day``) en o después de
    ``on`` -- el corte al que "cae" una compra hecha ese día."""
    cutoff = _clamped_date(on.year, on.month, billing_cycle_day)
    if cutoff < on:
        cutoff = _next_cutoff(billing_cycle_day, cutoff)
    return cutoff


def installment_schedule(purchase) -> list[dict]:
    """Calendario de cuotas de ``purchase``, anclado a los cortes de
    facturación de su tarjeta (``purchase.wallet``) -- no al día calendario
    de la compra. La cuota 1 cae en el primer corte en o después de
    `start_date` (igual que en un estado de cuenta real); cada cuota
    siguiente, un corte más adelante. Los montos vienen de
    `installment_amounts` (ceiling por cuota, la última es el resto).

    Devuelve ``[]`` si `purchase.wallet` no tiene `billing_cycle_day`
    (no debería pasar -- el serializer lo exige al crear)."""
    from apps.transactions.services import installment_amounts

    wallet = purchase.wallet
    if not wallet.billing_cycle_day:
        return []

    amounts = installment_amounts(purchase.total_amount, purchase.installments_total)
    cutoff = _cutoff_on_or_after(wallet.billing_cycle_day, purchase.start_date)
    schedule = []
    for n, amount in enumerate(amounts, start=1):
        schedule.append({"n": n, "cutoff_date": cutoff, "amount": amount})
        cutoff = _next_cutoff(wallet.billing_cycle_day, cutoff)
    return schedule


def installment_status(purchase, as_of=None) -> dict:
    """Estado de ``purchase`` a la fecha ``as_of`` (hoy por defecto): cuántas
    cuotas ya "vencieron" (su corte ya pasó) y cuánto queda -- puramente
    calculado, sin ningún contador que haya que ir avanzando a mano."""
    as_of = as_of or timezone.localdate()
    schedule = installment_schedule(purchase)
    pending = [s for s in schedule if s["cutoff_date"] > as_of]
    paid = len(schedule) - len(pending)
    return {
        "schedule": schedule,
        "installments_paid": paid,
        "is_completed": not pending,
        "remaining_amount": sum((s["amount"] for s in pending), Decimal("0")),
        "current_installment_amount": pending[0]["amount"] if pending else Decimal("0"),
        "next_due_date": pending[0]["cutoff_date"] if pending else None,
    }


def _card_balance_at(wallet, until_date) -> Decimal:
    """Saldo PROPIO de ``wallet`` contando solo transacciones con fecha <=
    ``until_date`` -- misma convención de signo que `recompute_wallet_balance`
    (negativo = deuda). ``-_card_balance_at(...)`` es el saldo usado de la
    tarjeta (``límite - disponible``) a esa fecha."""
    from apps.transactions.models import Transaction

    out = Transaction.objects.filter(wallet=wallet, date__lte=until_date).aggregate(
        total=Sum(
            Case(
                When(type=Transaction.TYPE_INCOME, then=F("amount")),
                default=-F("amount"),
                output_field=_MONEY,
            )
        )
    )["total"] or Decimal("0")
    incoming = Transaction.objects.filter(
        to_wallet=wallet, type=Transaction.TYPE_TRANSFER, date__lte=until_date
    ).aggregate(total=Sum("amount", output_field=_MONEY))["total"] or Decimal("0")
    return wallet.opening_balance + out + incoming


def _installment_pending(wallet, cutoff_date):
    """Capital a plazo que aún NO vence, a ``cutoff_date``: el total de cada
    compra a plazo de ``wallet`` ya bajó el disponible al registrarse (es una
    Transaction normal), pero al corte solo se debe lo que ya venció -- este
    monto se RESTA del pago de contado. ``lines`` es una fila por compra con
    cuotas pendientes, para mostrar el detalle."""
    from apps.transactions.models import InstallmentPurchase

    financed_not_due = Decimal("0")
    lines = []
    for purchase in InstallmentPurchase.objects.filter(wallet=wallet).select_related(
        "category"
    ):
        status = installment_status(purchase, as_of=cutoff_date)
        pending = status["schedule"][status["installments_paid"] :]
        if not pending:
            continue
        financed_not_due += status["remaining_amount"]
        lines.append(
            {
                "id": purchase.id,
                "description": purchase.description,
                "installments_pending": len(pending),
                "installments_total": purchase.installments_total,
                "amount_pending": status["remaining_amount"],
            }
        )
    return financed_not_due, lines


def credit_card_statement(wallet, as_of=None):
    """Pago de contado de la tarjeta a la fecha ``as_of`` (hoy por defecto):
    cuánto hay que abonar para dejar en cero lo que ya venció, sin adelantar
    las cuotas a plazo que todavía no vencen. ``None`` si `wallet` no es una
    tarjeta de crédito con fecha de corte (`kind=credit` + `billing_cycle_day`).

        pago_de_contado = saldo_usado                       (= límite - disponible)
                        - capital_a_plazo_aún_no_vencido

    ``saldo_usado`` es ``-_card_balance_at(wallet, as_of)`` -- exactamente lo
    que la app tiene como saldo de la tarjeta (``current_balance`` cuando
    ``as_of`` es hoy). El disponible sale de ahí: ``límite + saldo``. Cuando el
    saldo cacheado es correcto, el pago de contado también.

    Las cuotas se cuentan "vencidas" por el CORTE de la tarjeta, no por el
    calendario de la compra: cuota ``n`` vence en su ``n``-ésimo corte desde
    la compra (ver `installment_schedule`). No hay ningún registro manual de
    por medio -- es puro cálculo sobre `InstallmentPurchase` + los cortes de
    `wallet`.
    """
    if wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day:
        return None

    as_of = as_of or timezone.localdate()
    cutoff_date = _cutoff_on_or_before(wallet.billing_cycle_day, as_of)
    next_cutoff_date = _next_cutoff(wallet.billing_cycle_day, cutoff_date)
    payment_due_date = _payment_due_date(wallet, cutoff_date)

    balance = _card_balance_at(wallet, as_of)
    used = -balance
    available = (
        wallet.credit_limit + balance if wallet.credit_limit is not None else None
    )

    financed_not_due, lines = _installment_pending(wallet, cutoff_date)
    total_due = used - financed_not_due

    return {
        "cutoff_date": cutoff_date,
        "next_cutoff_date": next_cutoff_date,
        "payment_due_date": payment_due_date,
        "credit_limit": wallet.credit_limit,
        "available": available,
        "used": used,
        "installments_not_due": financed_not_due,
        "total_due": total_due,
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
