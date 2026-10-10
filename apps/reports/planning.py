"""Reportes para decidir: quién gastó cuánto en un presupuesto compartido,
"¿me alcanza?" antes de una compra y el resumen de la semana.

Van aparte de `services.py` (que ya es largo y es sobre todo cierre de mes e
insights) pero usan las mismas piezas: `visible_transactions` para respetar
las carteras privadas, `budget_vs_actual` para el presupuesto y
`upcoming_scheduled` para lo que ya está comprometido.
"""
import datetime as dt
from decimal import Decimal

from django.utils import timezone

from apps.common import periods
from apps.transactions.models import Category, Transaction
from apps.transactions.services import visible_transactions
from apps.workspaces.currency import convert, get_rate_map
from apps.workspaces.models import Membership

from .services import _OUTFLOW_Q, budget_vs_actual, upcoming_scheduled

ZERO = Decimal("0")


def _month_bounds(year, month):
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def member_spending(workspace, user, year, month):
    """Gasto del mes por miembro del workspace.

    "Quién gastó" es quien PAGÓ si la transacción tiene `paid_by` ligado a un
    miembro (una división entre personas), y si no, quien la cargó
    (`created_by`). Lo que no tiene ninguno de los dos (importaciones viejas,
    recurrentes creados sin usuario) va a un renglón "Sin asignar" para que la
    suma cierre con el gasto total del mes.
    """
    start, end = _month_bounds(year, month)
    rate_map = get_rate_map(workspace)

    members = {
        m.user_id: m
        for m in Membership.objects.filter(workspace=workspace).select_related("user")
    }
    rows = {
        uid: {
            "user": uid,
            "name": m.user.get_full_name() or m.user.username,
            "spent": ZERO,
            "count": 0,
        }
        for uid, m in members.items()
    }
    unassigned = {"user": None, "name": "Sin asignar", "spent": ZERO, "count": 0}

    qs = (
        visible_transactions(workspace, user)
        .filter(date__gte=start, date__lte=end)
        .filter(_OUTFLOW_Q)
        .values("amount", "currency", "created_by_id", "paid_by__member__user_id")
    )
    total = ZERO
    for t in qs:
        amount = convert(t["amount"], t["currency"], rate_map)
        if amount is None:
            continue
        who = t["paid_by__member__user_id"] or t["created_by_id"]
        row = rows.get(who, unassigned)
        row["spent"] += amount
        row["count"] += 1
        total += amount

    result = sorted(rows.values(), key=lambda r: -r["spent"])
    if unassigned["count"]:
        result.append(unassigned)
    for r in result:
        r["share_pct"] = float(r["spent"] / total * 100) if total else 0.0
    return {
        "year": year,
        "month": month,
        "base_currency": workspace.base_currency,
        "total": total,
        "members": result,
    }


def _committed_until(workspace, user, until):
    """Gastos programados (recurrentes y cuotas) desde hoy hasta `until` que
    todavía no se registraron. No entran el pago de tarjeta ni el vencimiento
    de una deuda: eso es pagar algo que ya se gastó, no gasto nuevo."""
    total = ZERO
    for item in upcoming_scheduled(workspace, user, until=until):
        if item["kind"] in ("recurring", "installment") and item["type"] == "expense":
            total += item["amount"]
    return total


def can_afford(workspace, user, amount, category=None, today=None):
    """¿Me alcanza para gastar `amount` (en la moneda base) hoy?

    Mira tres cosas, de la más concreta a la más general:

    1. La categoría: cuánto le queda de presupuesto en el período y cuánto le
       quedaría después (con la provisión acumulada, igual que el aviso de
       presupuesto).
    2. El presupuesto entero del período, menos lo programado que falta
       (recurrentes y cuotas hasta fin de período).
    3. Si no hay ningún presupuesto cargado, el flujo del mes: ingresos menos
       gastos menos lo programado.

    `verdict`: "ok", "tight" (alcanza pero deja la categoría o el total por
    debajo del 10%) u "over" (no alcanza).
    """
    today = today or timezone.localdate()
    period_start = periods.period_start(today, workspace.budget_period)
    period_end = periods.period_end(period_start, workspace.budget_period)
    report = budget_vs_actual(workspace, user, period_start)
    committed = _committed_until(workspace, user, period_end)

    category_info = None
    if category is not None:
        row = next((r for r in report["rows"] if r["category"] == str(category.id)), None)
        budgeted = (row["budgeted"] + row["provision"]) if row else ZERO
        spent = row["spent"] if row else ZERO
        category_info = {
            "category": str(category.id),
            "category_name": category.name,
            "budgeted": budgeted,
            "spent": spent,
            "remaining_before": budgeted - spent,
            "remaining_after": budgeted - spent - amount,
            "has_budget": budgeted > 0,
        }

    total_budgeted = sum((r["budgeted"] + r["provision"] for r in report["rows"]), ZERO)
    if total_budgeted > 0:
        basis = "budget"
        available = total_budgeted - report["totals"]["spent"] - committed
        reference = total_budgeted
    else:
        # Sin presupuesto: lo que entró este mes menos lo que salió.
        basis = "cashflow"
        start, end = _month_bounds(today.year, today.month)
        rate_map = get_rate_map(workspace)
        income = expense = ZERO
        for t in (
            visible_transactions(workspace, user)
            .filter(date__gte=start, date__lte=end)
            .values("type", "amount", "currency", "category_id")
        ):
            value = convert(t["amount"], t["currency"], rate_map)
            if value is None:
                continue
            if t["type"] == Transaction.TYPE_INCOME:
                income += value
            elif t["type"] == Transaction.TYPE_EXPENSE or (
                t["type"] == Transaction.TYPE_TRANSFER and t["category_id"]
            ):
                expense += value
        committed = _committed_until(workspace, user, end)
        available = income - expense - committed
        reference = income

    available_after = available - amount

    verdict = "ok"
    if available_after < 0 or (
        category_info and category_info["has_budget"] and category_info["remaining_after"] < 0
    ):
        verdict = "over"
    else:
        tight_total = reference > 0 and available_after < reference * Decimal("0.10")
        tight_category = (
            category_info
            and category_info["has_budget"]
            and category_info["remaining_after"] < category_info["budgeted"] * Decimal("0.10")
        )
        if tight_total or tight_category:
            verdict = "tight"

    return {
        "amount": amount,
        "base_currency": workspace.base_currency,
        "period_start": period_start,
        "period_end": period_end,
        "days_left": (period_end - today).days + 1,
        "basis": basis,
        "committed": committed,
        "available_before": available,
        "available_after": available_after,
        "category": category_info,
        "verdict": verdict,
    }


def weekly_summary(workspace, user, today=None):
    """Datos del resumen de la semana que terminó ayer (lunes a domingo, si
    corre el lunes): total gastado, comparación con la semana anterior, la
    categoría donde más se fue y cuántas categorías ya pasaron su
    presupuesto. `None` si en la semana no hubo gastos (no se avisa nada)."""
    today = today or timezone.localdate()
    week_end = today - dt.timedelta(days=1)
    week_start = week_end - dt.timedelta(days=6)
    prev_start = week_start - dt.timedelta(days=7)
    rate_map = get_rate_map(workspace)

    def spent_between(start, end):
        by_cat = {}
        total = ZERO
        for t in (
            visible_transactions(workspace, user)
            .filter(date__gte=start, date__lte=end)
            .filter(_OUTFLOW_Q)
            .values("amount", "currency", "category_id")
        ):
            value = convert(t["amount"], t["currency"], rate_map)
            if value is None:
                continue
            total += value
            by_cat[t["category_id"]] = by_cat.get(t["category_id"], ZERO) + value
        return total, by_cat

    total, by_cat = spent_between(week_start, week_end)
    if total <= 0:
        return None
    prev_total, _ = spent_between(prev_start, week_start - dt.timedelta(days=1))

    top_category_name, top_amount = None, ZERO
    if by_cat:
        top_id, top_amount = max(by_cat.items(), key=lambda kv: kv[1])
        if top_id:
            top_category_name = (
                Category.objects.filter(id=top_id).values_list("name", flat=True).first()
            )

    report = budget_vs_actual(
        workspace, user, periods.period_start(today, workspace.budget_period)
    )
    over_budget = [
        r["category_name"]
        for r in report["rows"]
        if (r["budgeted"] + r["provision"]) > 0 and r["spent"] > r["budgeted"] + r["provision"]
    ]

    change_pct = float((total - prev_total) / prev_total * 100) if prev_total > 0 else None
    return {
        "week_start": week_start,
        "week_end": week_end,
        "total": total,
        "previous_total": prev_total,
        "change_pct": change_pct,
        "top_category_name": top_category_name,
        "top_category_amount": top_amount,
        "over_budget": over_budget,
    }
