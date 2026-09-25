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
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Case, DecimalField, F, Q, Sum, When
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Wallet

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def aggregated_balances(workspace_id) -> dict:
    """Saldo propio + el de todos los descendientes, de TODAS las carteras del
    workspace, con una sola consulta: ``{wallet_id: saldo agregado}``.

    Lo que hace `Wallet.aggregated_balance` cartera por cartera, pero sin una
    consulta por nodo del árbol. En un listado eso eran decenas de consultas a
    ~25 ms cada una (N+1 de /wallets/ en Sentry). Cuenta las mismas carteras que
    `wallet.children` -- las no borradas, sin mirar visibilidad ni archivadas.
    """
    balance: dict = {}
    children = defaultdict(list)
    rows = Wallet.objects.filter(workspace_id=workspace_id).values_list(
        "id", "parent_id", "current_balance"
    )
    for wallet_id, parent_id, current in rows:
        balance[wallet_id] = current
        children[parent_id].append(wallet_id)

    totals: dict = {}

    def total(wallet_id, trail):
        if wallet_id in totals:
            return totals[wallet_id]
        if wallet_id in trail:  # un ciclo no debería existir (lo impide `validate_parent`)
            return balance[wallet_id]
        trail.add(wallet_id)
        result = balance[wallet_id] + sum(
            (total(child, trail) for child in children.get(wallet_id, ())), Decimal("0")
        )
        trail.discard(wallet_id)
        totals[wallet_id] = result
        return result

    for wallet_id in balance:
        total(wallet_id, set())
    return totals


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


def _balance_as_of(wallet, until_date=None) -> Decimal:
    """Saldo PROPIO de ``wallet``: opening_balance + Σ transacciones vivas
    (income +, expense -, transfer saliente -, entrante +), contando solo
    hasta ``until_date`` inclusive (todas, si es ``None``). Única fuente de
    la convención de signo -- tanto `recompute_wallet_balance` (todo el
    historial) como `credit_card_statement` (a una fecha) parten de acá para
    no arriesgarse a que diverjan si esta regla cambia."""
    from apps.transactions.models import Transaction

    until = {"date__lte": until_date} if until_date is not None else {}

    out = Transaction.objects.filter(wallet=wallet, **until).aggregate(
        total=Sum(
            Case(
                When(type=Transaction.TYPE_INCOME, then=F("amount")),
                default=-F("amount"),
                output_field=_MONEY,
            )
        )
    )["total"] or Decimal("0")

    incoming = Transaction.objects.filter(
        to_wallet=wallet, type=Transaction.TYPE_TRANSFER, **until
    ).aggregate(total=Sum("amount", output_field=_MONEY))["total"] or Decimal("0")

    return wallet.opening_balance + out + incoming


def recompute_wallet_balance(wallet) -> Decimal:
    """Recalcula `current_balance` (propio) desde cero: opening_balance + Σ
    transacciones vivas (income +, expense -, transfer saliente -, entrante +)."""
    wallet.current_balance = _balance_as_of(wallet)
    wallet.save(update_fields=["current_balance", "updated_at"])
    return wallet.current_balance


_CENTS = Decimal("0.01")


def _savings_annual_rate(wallet) -> Decimal:
    """Tasa anual (fracción, no %) que gana `wallet`, sea cual sea el período
    en que el usuario la cargó."""
    rate = wallet.savings_interest_rate or Decimal("0")
    if wallet.savings_interest_rate_period == Wallet.INTEREST_PERIOD_MONTHLY:
        rate = rate * 12
    return rate / 100


def _is_compounding_boundary(day: date_cls, compounding: str) -> bool:
    """Si en `day` el interés acumulado desde la última vez se suma a la base
    sobre la que se calcula el interés del día siguiente (interés
    compuesto)."""
    last_day_of_month = calendar.monthrange(day.year, day.month)[1]
    if compounding == Wallet.COMPOUNDING_DAILY:
        return True
    if compounding == Wallet.COMPOUNDING_BIWEEKLY:
        return day.day in (15, last_day_of_month)
    if compounding == Wallet.COMPOUNDING_MONTHLY:
        return day.day == last_day_of_month
    if compounding == Wallet.COMPOUNDING_ANNUAL:
        return day.month == 12 and day.day == 31
    return False


def savings_interest_projection(wallet, year: int, month: int) -> dict:
    """Reporte de interés estimado para `wallet` (debe ser `purpose=savings`
    y tener `savings_interest_rate` configurada) en el mes `year`-`month`.

    Usa saldo diario ponderado: cada día del mes se calcula interés sobre el
    saldo REAL de ese día (reconstruido de las transacciones vivas, igual que
    `_balance_as_of`), más cualquier interés ya capitalizado en días
    anteriores según `savings_interest_compounding`. Para los días del mes
    que todavía no ocurrieron (mes en curso o futuro), se asume que el saldo
    se mantiene igual al último saldo real conocido -- no proyectamos
    depósitos/retiros que el usuario no hizo todavía.
    """
    if wallet.purpose != Wallet.PURPOSE_SAVINGS:
        raise ValueError("Solo aplica a carteras de ahorro (purpose=savings).")

    days_in_month = calendar.monthrange(year, month)[1]
    first_day = date_cls(year, month, 1)
    last_day = date_cls(year, month, days_in_month)
    today = timezone.localdate()

    opening_balance = _balance_as_of(wallet, first_day - timedelta(days=1))
    annual_rate = _savings_annual_rate(wallet)
    daily_rate = annual_rate / 365

    compounded_base = Decimal("0")  # interés ya capitalizado, gana interés a su vez
    pending = Decimal("0")  # interés de este período de capitalización, aún sin sumar a la base
    total_interest = Decimal("0")
    last_known_balance = opening_balance
    closing_balance = opening_balance

    cursor = first_day
    while cursor <= last_day:
        if cursor <= today:
            last_known_balance = _balance_as_of(wallet, cursor)
        balance = last_known_balance
        closing_balance = balance

        if daily_rate:
            effective_balance = balance + compounded_base
            day_interest = effective_balance * daily_rate
            total_interest += day_interest
            pending += day_interest
            if _is_compounding_boundary(cursor, wallet.savings_interest_compounding):
                compounded_base += pending
                pending = Decimal("0")

        cursor += timedelta(days=1)

    return {
        "wallet_id": wallet.id,
        "year": year,
        "month": month,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "annual_rate_pct": (annual_rate * 100).quantize(_CENTS, rounding=ROUND_HALF_UP),
        "compounding": wallet.savings_interest_compounding,
        "estimated_interest": total_interest.quantize(_CENTS, rounding=ROUND_HALF_UP),
        "is_partial_month": today < last_day,
    }


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

    ``saldo_usado`` es ``-_balance_as_of(wallet, as_of)`` -- exactamente lo
    que la app tiene como saldo de la tarjeta (``current_balance`` cuando
    ``as_of`` es hoy). El disponible sale de ahí: ``límite + saldo``, sin
    superar el límite (un saldo a favor no es "más disponible que el límite").
    Cuando el saldo cacheado es correcto, el pago de contado también.

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

    balance = _balance_as_of(wallet, as_of)
    used = -balance
    available = (
        min(wallet.credit_limit, wallet.credit_limit + balance)
        if wallet.credit_limit is not None
        else None
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


def _flows_between(wallet, after, until, *, exclude_installments=False):
    """(abonos, cargos) de ``wallet`` con fecha en ``(after, until]``:
    abonos = ingresos + transferencias entrantes; cargos = gastos +
    transferencias salientes. En una tarjeta, abonos son los pagos y cargos
    las compras. ``exclude_installments`` deja afuera la transacción del
    total de cada compra a plazo (en un estado se cobra cuota por cuota)."""
    from apps.transactions.models import Transaction

    window = {"date__gt": after, "date__lte": until}
    own = Transaction.objects.filter(wallet=wallet, **window)
    if exclude_installments:
        own = own.filter(installment_purchase__isnull=True)
    rows = own.values("type").annotate(total=Sum("amount", output_field=_MONEY))
    credits = debits = Decimal("0")
    for r in rows:
        if r["type"] == Transaction.TYPE_INCOME:
            credits += r["total"]
        else:  # gasto o transferencia saliente
            debits += r["total"]
    credits += Transaction.objects.filter(
        to_wallet=wallet, type=Transaction.TYPE_TRANSFER, **window
    ).aggregate(total=Sum("amount", output_field=_MONEY))["total"] or Decimal("0")
    return credits, debits


def _credits_between(wallet, after, until) -> Decimal:
    """Abonos a ``wallet`` con fecha en ``(after, until]`` -- en una tarjeta,
    los pagos hechos desde un corte."""
    return _flows_between(wallet, after, until)[0]


def _installments_due_on(wallet, cutoff_date) -> Decimal:
    """Cuotas de las compras a plazo de ``wallet`` que se cobran en el corte
    ``cutoff_date`` (ver `installment_schedule`)."""
    from apps.transactions.models import InstallmentPurchase

    total = Decimal("0")
    for purchase in InstallmentPurchase.objects.filter(wallet=wallet, start_date__lte=cutoff_date):
        for row in installment_schedule(purchase):
            if row["cutoff_date"] == cutoff_date:
                total += row["amount"]
    return total


def _money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def minimum_payment(wallet, statement_balance):
    """Pago mínimo de un estado: ``max(piso, % del saldo)``, nunca más que el
    saldo. ``None`` si la tarjeta no tiene ni % ni piso configurado -- cada
    banco lo calcula distinto y no inventamos una regla."""
    if wallet.min_payment_pct is None and wallet.min_payment_floor is None:
        return None
    if statement_balance <= 0:
        return Decimal("0.00")
    by_pct = statement_balance * (wallet.min_payment_pct or Decimal("0")) / 100
    floor = wallet.min_payment_floor or Decimal("0")
    return _money(min(max(by_pct, floor), statement_balance))


def _monthly_interest(wallet, principal):
    """Interés de un mes sobre ``principal`` con la tasa anual de la tarjeta
    (`interest_rate`, %). ``None`` sin tasa configurada."""
    if not wallet.interest_rate:
        return None
    if principal <= 0:
        return Decimal("0.00")
    return _money(principal * wallet.interest_rate / 100 / 12)


STATUS_PAID = "paid"
STATUS_MINIMUM_PAID = "minimum_paid"
STATUS_PENDING = "pending"
STATUS_OVERDUE = "overdue"
STATUS_NOTHING_DUE = "nothing_due"


def statement_cycle(wallet, cutoff_date, as_of=None):
    """Estado de cuenta del ciclo que cierra en ``cutoff_date``, como lo
    imprime un banco:

        saldo_anterior + compras + cuotas_del_ciclo - pagos (+ ajustes)
            = saldo_al_corte (pago de contado)

    - ``previous_balance``: saldo al corte anterior.
    - ``purchases``: compras y cargos del ciclo, SIN el total de las compras
      a plazo (esas entran cuota por cuota, en ``installments_charged``).
    - ``payments``: pagos hechos DENTRO del ciclo.
    - ``adjustments``: lo que haga falta para cuadrar con el saldo real (0
      salvo casos raros: un saldo inicial editado, una compra a plazo sin su
      transacción...). Se expone en vez de esconderlo.
    - ``paid_since_cutoff``: pagos hechos DESPUÉS del corte (hasta
      ``as_of`` o la fecha límite, lo que llegue primero) -- son los que
      cuentan para este estado.

    ``None`` si la tarjeta no tiene fecha de corte."""
    if wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day:
        return None
    as_of = as_of or timezone.localdate()
    prev_cutoff = _cutoff_on_or_before(wallet.billing_cycle_day, cutoff_date - timedelta(days=1))
    due_date = _payment_due_date(wallet, cutoff_date)

    balance = max(credit_card_statement(wallet, as_of=cutoff_date)["total_due"], Decimal("0"))
    previous = max(credit_card_statement(wallet, as_of=prev_cutoff)["total_due"], Decimal("0"))
    payments, purchases = _flows_between(wallet, prev_cutoff, cutoff_date, exclude_installments=True)
    installments = _installments_due_on(wallet, cutoff_date)
    adjustments = balance - (previous + purchases + installments - payments)

    pay_until = min(as_of, due_date) if due_date else as_of
    paid = _credits_between(wallet, cutoff_date, pay_until) if pay_until > cutoff_date else Decimal("0")
    remaining = max(balance - paid, Decimal("0"))
    minimum = minimum_payment(wallet, balance)
    minimum_remaining = max(minimum - paid, Decimal("0")) if minimum is not None else None

    if balance <= 0:
        status = STATUS_NOTHING_DUE
    elif remaining <= 0:
        status = STATUS_PAID
    elif due_date and as_of > due_date:
        status = STATUS_OVERDUE if minimum_remaining is None or minimum_remaining > 0 else STATUS_MINIMUM_PAID
    elif minimum_remaining is not None and minimum_remaining <= 0:
        status = STATUS_MINIMUM_PAID
    else:
        status = STATUS_PENDING

    return {
        "period_start": prev_cutoff + timedelta(days=1),
        "cutoff_date": cutoff_date,
        "payment_due_date": due_date,
        "previous_balance": previous,
        "purchases": purchases,
        "installments_charged": installments,
        "payments": payments,
        "adjustments": adjustments,
        "statement_balance": balance,
        "minimum_payment": minimum,
        "paid_since_cutoff": paid,
        "remaining": remaining,
        "minimum_remaining": minimum_remaining,
        "status": status,
        # Estimación simple (un mes, tasa anual / 12) sobre lo que quedaría
        # financiado: pagando sólo el mínimo, o lo que falta hoy.
        "interest_if_minimum": (
            _monthly_interest(wallet, balance - minimum) if minimum is not None else None
        ),
        "interest_if_unpaid": _monthly_interest(wallet, remaining),
    }


def statement_cycles(wallet, count=6, as_of=None):
    """Los últimos ``count`` estados (el más reciente primero)."""
    if wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day:
        return []
    as_of = as_of or timezone.localdate()
    cutoff = _cutoff_on_or_before(wallet.billing_cycle_day, as_of)
    cycles = []
    for _ in range(count):
        cycles.append(statement_cycle(wallet, cutoff, as_of=as_of))
        cutoff = _cutoff_on_or_before(wallet.billing_cycle_day, cutoff - timedelta(days=1))
    return cycles


def unbilled_activity(wallet, as_of=None):
    """Lo que va al PRÓXIMO estado: compras desde el último corte (sin el
    total de las compras a plazo) + las cuotas que se cobran en el próximo
    corte. No se paga ahora -- es la otra mitad de "del corte / después del
    corte"."""
    as_of = as_of or timezone.localdate()
    cutoff = _cutoff_on_or_before(wallet.billing_cycle_day, as_of)
    next_cutoff = _next_cutoff(wallet.billing_cycle_day, cutoff)
    _, purchases = _flows_between(wallet, cutoff, as_of, exclude_installments=True)
    installments = _installments_due_on(wallet, next_cutoff)
    return {
        "since": cutoff + timedelta(days=1),
        "next_cutoff_date": next_cutoff,
        "purchases": purchases,
        "installments_next": installments,
        "total": purchases + installments,
    }


def statement_payoff(wallet, as_of=None):
    """Lo que falta pagar del ÚLTIMO corte para no generar intereses -- ver
    `statement_cycle`. ``None`` si la tarjeta no tiene corte o fecha de pago
    configurados."""
    if wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day or not wallet.payment_due_day:
        return None
    as_of = as_of or timezone.localdate()
    return statement_cycle(wallet, _cutoff_on_or_before(wallet.billing_cycle_day, as_of), as_of=as_of)


def _previous_period(start, end):
    """El período anterior de igual forma: el mes calendario anterior si
    ``start..end`` es un mes completo; si no, los mismos días justo antes."""
    last_day = calendar.monthrange(end.year, end.month)[1]
    if start.day == 1 and end.day == last_day and start.year == end.year and start.month == end.month:
        prev_end = start - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def wallet_period_summary(wallet, start, end):
    """Saldo inicial → entradas → salidas → saldo final de ``wallet`` entre
    ``start`` y ``end`` (inclusive), como un extracto bancario, más las
    mismas entradas/salidas del período anterior para comparar. Cuenta
    transferencias (entrantes/salientes): acá importa el saldo, no el
    presupuesto."""
    from apps.transactions.models import Transaction

    opening = _balance_as_of(wallet, start - timedelta(days=1))
    inflows, outflows = _flows_between(wallet, start - timedelta(days=1), end)
    prev_start, prev_end = _previous_period(start, end)
    prev_in, prev_out = _flows_between(wallet, prev_start - timedelta(days=1), prev_end)
    count = Transaction.objects.filter(
        Q(wallet=wallet) | Q(to_wallet=wallet), date__gte=start, date__lte=end
    ).count()
    return {
        "date_after": start,
        "date_before": end,
        "opening_balance": opening,
        "inflows": inflows,
        "outflows": outflows,
        "closing_balance": opening + inflows - outflows,
        "count": count,
        "previous": {
            "date_after": prev_start,
            "date_before": prev_end,
            "inflows": prev_in,
            "outflows": prev_out,
        },
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
