"""Cierre de mes: snapshot financiero + rollover de provisiones.

Lo dispara la tarea de Celery Beat el día 1 (para el mes anterior), pero se
puede llamar a mano para cualquier (año, mes).
"""
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import F, Q
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)
from apps.transactions.services import _advance, visible_transactions
from apps.workspaces.currency import convert, get_rate_map
from apps.workspaces.models import Workspace

from .models import MonthlySnapshot


def _sum_converted(qs, rate_map, amount_field="amount", currency_field="currency"):
    """Como `_sum`, pero convirtiendo cada fila a la moneda base antes de
    sumar -- `Sum()` de la base de datos no puede aplicar una tasa distinta
    por fila. Las filas en una moneda sin tasa configurada simplemente no
    se cuentan (ver `apps.workspaces.currency.convert`)."""
    total = Decimal("0")
    for row in qs.values(amount_field, currency_field):
        converted = convert(row[amount_field], row[currency_field], rate_map)
        if converted is not None:
            total += converted
    return total


def _visible(qs, user):
    if user is None:
        return qs
    return qs.filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))


def net_worth_breakdown(workspace, user=None) -> dict:
    """
    Patrimonio neto + totales por tipo de cartera, convertidos a
    ``workspace.base_currency`` (ver ``apps.workspaces.currency``) -- una
    cartera en una moneda sin tasa configurada no entra en ningún total.

    - ``net``       = Σ ``current_balance`` (propio, sin hijos, para no doble-contar)
                      de las carteras activas con ``counts_toward_net_worth=True``.
    - ``by_purpose``= Σ ``current_balance`` por ``purpose``, ignorando el flag.

    Con ``user`` se excluyen las carteras privadas de las que no es owner.
    """
    # Incluye las archivadas: siguen sumando al patrimonio neto (como en Buddy).
    wallets = list(
        _visible(Wallet.objects.filter(workspace=workspace), user)
    )
    rate_map = get_rate_map(workspace)
    converted = [
        (w, convert(w.current_balance, w.currency, rate_map)) for w in wallets
    ]

    net = sum(
        (c for w, c in converted if w.counts_toward_net_worth and c is not None),
        Decimal("0"),
    )
    by_purpose = {
        purpose: sum(
            (c for w, c in converted if w.purpose == purpose and c is not None),
            Decimal("0"),
        )
        for purpose, _ in Wallet.PURPOSE_CHOICES
    }
    return {"net": net, "by_purpose": by_purpose, "base_currency": workspace.base_currency}


def net_worth(workspace) -> Decimal:
    return net_worth_breakdown(workspace)["net"]


# Dinero que "sale" y se clasifica: gastos + transferencias con categoría
# (p. ej. mover a ahorro). No cuenta ingresos ni transferencias sin categoría.
_OUTFLOW_Q = Q(type=Transaction.TYPE_EXPENSE) | Q(
    type=Transaction.TYPE_TRANSFER, category__isnull=False
)


def spending_by_category(workspace, user, year, month):
    rate_map = get_rate_map(workspace)
    rows = (
        visible_transactions(workspace, user)
        .filter(date__year=year, date__month=month)
        .filter(_OUTFLOW_Q, category__isnull=False)
        .values("category_id", "category__name", "amount", "currency")
    )

    totals: dict[str, dict] = {}
    order = []
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None:
            continue
        key = str(r["category_id"])
        if key not in totals:
            totals[key] = {
                "category": key,
                "category_name": r["category__name"],
                "spent": Decimal("0"),
            }
            order.append(key)
        totals[key]["spent"] += converted

    result = [totals[k] for k in order]
    result.sort(key=lambda r: -r["spent"])
    return result


def budget_vs_actual(workspace, user, year, month):
    """Presupuesto vs. gasto real por categoría para un mes.

    ``budgeted`` se asume siempre en ``workspace.base_currency`` (el monto
    del presupuesto no está atado a ninguna cartera); ``spent`` se convierte
    desde la moneda de cada transacción -- ver ``apps.workspaces.currency``.
    """
    rate_map = get_rate_map(workspace)
    budgets = {
        b.category_id: b.amount
        for b in CategoryBudget.objects.filter(workspace=workspace, year=year, month=month)
    }
    spent: dict = {}
    for row in (
        visible_transactions(workspace, user)
        .filter(date__year=year, date__month=month, counts_toward_budget=True)
        .filter(_OUTFLOW_Q)
        .values("category", "amount", "currency")
    ):
        converted = convert(row["amount"], row["currency"], rate_map)
        if converted is None:
            continue
        spent[row["category"]] = spent.get(row["category"], Decimal("0")) + converted
    provisions = {
        p.category_id: p.accumulated_amount
        for p in CategoryProvision.objects.filter(category__workspace=workspace)
    }

    cat_ids = set(budgets) | set(spent)
    cats = {
        str(c.id): c
        for c in Category.objects.filter(id__in=cat_ids).select_related("parent")
    }

    rows = []
    for cid in cat_ids:
        budgeted = budgets.get(cid, Decimal("0"))
        used = spent.get(cid, Decimal("0"))
        cat = cats.get(str(cid))
        rows.append(
            {
                "category": str(cid),
                "category_name": cat.name if cat else None,
                "budgeted": budgeted,
                "spent": used,
                "remaining": budgeted - used,
                "provision": provisions.get(cid, Decimal("0")),
            }
        )
    rows.sort(key=lambda r: (r["category_name"] or "").lower())

    totals = {
        "budgeted": sum((r["budgeted"] for r in rows), Decimal("0")),
        "spent": sum((r["spent"] for r in rows), Decimal("0")),
        "remaining": sum((r["remaining"] for r in rows), Decimal("0")),
    }
    groups = _group_budget_rows(rows, cats)
    return {
        "year": year,
        "month": month,
        "base_currency": workspace.base_currency,
        "rows": rows,
        "groups": groups,
        "totals": totals,
    }


def _group_budget_rows(rows, cats):
    """Agrupa las filas de presupuesto por su grupo (categoría padre).

    Una categoría sin padre es su propio grupo. El resultado alimenta el
    anillo "restante para gastar" y las tarjetas por grupo del cliente.
    """
    buckets = {}
    order = []
    for row in rows:
        cat = cats.get(row["category"])
        if cat is not None and cat.parent_id is not None:
            gid, gname = cat.parent_id, (cat.parent.name if cat.parent else None)
        elif cat is not None:
            gid, gname = cat.id, cat.name
        else:
            gid, gname = None, None
        key = str(gid) if gid else "__none__"
        if key not in buckets:
            buckets[key] = {
                "group": str(gid) if gid else None,
                "group_name": gname or "Sin grupo",
                "budgeted": Decimal("0"),
                "spent": Decimal("0"),
                "remaining": Decimal("0"),
                "rows": [],
            }
            order.append(key)
        b = buckets[key]
        b["rows"].append(row)
        b["budgeted"] += row["budgeted"]
        b["spent"] += row["spent"]
        b["remaining"] += row["remaining"]

    result = [buckets[k] for k in order]
    result.sort(key=lambda g: (g["group_name"] or "").lower())
    return result


def monthly_cashflow(workspace, user, months=6, until=None):
    until = (until or timezone.localdate()).replace(day=1)
    rate_map = get_rate_map(workspace)
    periods = []
    cursor = until
    for _ in range(months):
        periods.append((cursor.year, cursor.month))
        cursor -= relativedelta(months=1)
    periods.reverse()

    txns = visible_transactions(workspace, user)
    series = []
    for year, month in periods:
        month_txns = txns.filter(date__year=year, date__month=month)
        income = _sum_converted(month_txns.filter(type=Transaction.TYPE_INCOME), rate_map)
        expenses = _sum_converted(month_txns.filter(type=Transaction.TYPE_EXPENSE), rate_map)
        series.append(
            {
                "year": year,
                "month": month,
                "income": income,
                "expenses": expenses,
                "net": income - expenses,
            }
        )
    return series


def category_trends(workspace, user, months=6):
    """Gasto mensual por categoría de los últimos `months` meses + cuáles
    crecieron (o bajaron) más entre el mes en curso y el anterior.

    Reusa `spending_by_category` mes a mes -- son pocos meses (máx. 24) y el
    workspace de una app personal no tiene tantas categorías, así que
    N queries chicas es más simple que armar una sola con `TruncMonth`.
    """
    until = timezone.localdate().replace(day=1)
    periods = []
    cursor = until
    for _ in range(months):
        periods.append((cursor.year, cursor.month))
        cursor -= relativedelta(months=1)
    periods.reverse()

    by_cat: dict[str, dict] = {}
    for idx, (year, month) in enumerate(periods):
        for row in spending_by_category(workspace, user, year, month):
            key = row["category"]
            if key not in by_cat:
                by_cat[key] = {
                    "category": key,
                    "category_name": row["category_name"],
                    "amounts": [Decimal("0")] * months,
                }
            by_cat[key]["amounts"][idx] = row["spent"]

    categories = list(by_cat.values())
    for c in categories:
        last, prev = c["amounts"][-1], c["amounts"][-2] if months > 1 else Decimal("0")
        c["change"] = last - prev
        c["change_pct"] = float(c["change"] / prev * 100) if prev else None

    # El que más creció primero -- lo que interesa mostrar es "esto se te
    # disparó", no un orden alfabético ni por total.
    categories.sort(key=lambda c: c["change"], reverse=True)

    return {
        "months": [{"year": y, "month": m} for y, m in periods],
        "categories": categories,
    }


def dashboard_summary(workspace, user, today=None):
    today = today or timezone.localdate()
    from apps.email_import.models import EmailImportLog

    this_month = monthly_cashflow(workspace, user, months=1, until=today)[0]
    return {
        "month": this_month,
        "net_worth": net_worth_breakdown(workspace, user)["net"],
        "base_currency": workspace.base_currency,
        "pending_email_imports": EmailImportLog.objects.filter(
            workspace=workspace, status=EmailImportLog.STATUS_PENDING
        ).count(),
        "top_expense_categories": spending_by_category(
            workspace, user, today.year, today.month
        )[:5],
    }


def upcoming_scheduled(workspace, user, until=None, since=None):
    """Ocurrencias futuras de gastos recurrentes + cuotas, SIN crearlas.

    Alimenta la tarjeta "PROGRAMADO" y los marcadores de la lista. `since`
    por defecto hoy, `until` por defecto fin del mes en curso.
    """
    today = timezone.localdate()
    since = since or today
    if until is None:
        until = (today.replace(day=1) + relativedelta(months=1)) - relativedelta(days=1)

    def _wallet_ok(w):
        return w.visibility == Wallet.VISIBILITY_SHARED or w.owner_id == getattr(
            user, "id", None
        )

    items = []

    recurring = RecurringExpense.objects.filter(
        workspace=workspace, is_active=True, next_due_date__lte=until
    ).select_related("category", "wallet")
    for rec in recurring:
        if not _wallet_ok(rec.wallet):
            continue
        due = rec.next_due_date
        guard = 0
        while due <= until and guard < 400:
            guard += 1
            if due >= since:
                items.append(
                    {
                        "date": due,
                        "kind": "recurring",
                        "source_id": rec.id,
                        "description": rec.category.name,
                        "amount": rec.amount,
                        "category": rec.category_id,
                        "category_name": rec.category.name,
                        "wallet": rec.wallet_id,
                        "wallet_name": rec.wallet.name,
                    }
                )
            due = _advance(due, rec.frequency)

    installments = InstallmentPurchase.objects.filter(
        workspace=workspace
    ).select_related("category", "wallet")
    for pur in installments:
        if not _wallet_ok(pur.wallet):
            continue
        for n in range(pur.installments_paid + 1, pur.installments_total + 1):
            due = pur.start_date + relativedelta(months=n - 1)
            if due < since or due > until:
                continue
            items.append(
                {
                    "date": due,
                    "kind": "installment",
                    "source_id": pur.id,
                    "description": f"{pur.description} (cuota {n}/{pur.installments_total})",
                    "amount": pur.installment_amount,
                    "category": pur.category_id,
                    "category_name": pur.category.name,
                    "wallet": pur.wallet_id,
                    "wallet_name": pur.wallet.name,
                }
            )

    items.sort(key=lambda i: i["date"])
    return items


def close_month(year, month, workspace=None):
    """Crea/actualiza el MonthlySnapshot de cada workspace y hace el rollover."""
    workspaces = [workspace] if workspace is not None else Workspace.objects.all()
    snapshots = []

    for ws in workspaces:
        rate_map = get_rate_map(ws)
        month_txns = Transaction.objects.filter(
            wallet__workspace=ws, date__year=year, date__month=month
        )
        snapshot, _ = MonthlySnapshot.objects.update_or_create(
            workspace=ws,
            year=year,
            month=month,
            defaults={
                "total_net_worth": net_worth(ws),
                "total_income": _sum_converted(
                    month_txns.filter(type=Transaction.TYPE_INCOME), rate_map
                ),
                "total_expenses": _sum_converted(
                    month_txns.filter(type=Transaction.TYPE_EXPENSE), rate_map
                ),
            },
        )
        snapshots.append(snapshot)
        _rollover_provisions(ws, year, month)

    return snapshots


def _rollover_provisions(workspace, year, month):
    """Suma el sobrante (presupuesto - gasto real) de cada categoría a su provisión."""
    rate_map = get_rate_map(workspace)
    budgets = CategoryBudget.objects.filter(
        workspace=workspace, year=year, month=month
    ).select_related("category")

    for budget in budgets:
        spent = _sum_converted(
            Transaction.objects.filter(
                category=budget.category,
                date__year=year,
                date__month=month,
                counts_toward_budget=True,
            ),
            rate_map,
        )
        leftover = budget.amount - spent
        if leftover <= 0:
            continue

        provision, _ = CategoryProvision.objects.get_or_create(category=budget.category)
        CategoryProvision.objects.filter(pk=provision.pk).update(
            accumulated_amount=F("accumulated_amount") + leftover,
            last_updated=timezone.localdate(),
        )
