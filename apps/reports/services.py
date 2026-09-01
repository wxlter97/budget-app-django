"""Cierre de mes: snapshot financiero + rollover de provisiones.

Lo dispara la tarea de Celery Beat el día 1 (para el mes anterior), pero se
puede llamar a mano para cualquier (año, mes).
"""
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    Transaction,
)
from apps.transactions.services import visible_transactions
from apps.workspaces.models import Workspace

from .models import MonthlySnapshot


def _sum(qs, field):
    return qs.aggregate(s=Sum(field))["s"] or Decimal("0")


def _visible(qs, user):
    if user is None:
        return qs
    return qs.filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))


def net_worth_breakdown(workspace, user=None) -> dict:
    """
    Patrimonio neto + totales por tipo de cartera.

    - ``net``       = Σ ``current_balance`` (propio, sin hijos, para no doble-contar)
                      de las carteras activas con ``counts_toward_net_worth=True``.
    - ``by_purpose``= Σ ``current_balance`` por ``purpose``, ignorando el flag.

    Con ``user`` se excluyen las carteras privadas de las que no es owner.
    """
    wallets = list(
        _visible(Wallet.objects.filter(workspace=workspace, is_active=True), user)
    )
    net = sum(
        (w.current_balance for w in wallets if w.counts_toward_net_worth),
        Decimal("0"),
    )
    by_purpose = {
        purpose: sum(
            (w.current_balance for w in wallets if w.purpose == purpose),
            Decimal("0"),
        )
        for purpose, _ in Wallet.PURPOSE_CHOICES
    }
    return {"net": net, "by_purpose": by_purpose}


def net_worth(workspace) -> Decimal:
    return net_worth_breakdown(workspace)["net"]


def spending_by_category(workspace, user, year, month):
    rows = (
        visible_transactions(workspace, user)
        .filter(date__year=year, date__month=month, type=Transaction.TYPE_EXPENSE)
        .values("category_id", "category__name")
        .annotate(spent=Sum("amount"))
        .order_by("-spent")
    )
    return [
        {
            "category": str(r["category_id"]),
            "category_name": r["category__name"],
            "spent": r["spent"],
        }
        for r in rows
    ]


def budget_vs_actual(workspace, user, year, month):
    """Presupuesto vs. gasto real por categoría para un mes."""
    budgets = {
        b.category_id: b.amount
        for b in CategoryBudget.objects.filter(workspace=workspace, year=year, month=month)
    }
    spent = {
        row["category"]: row["spent"]
        for row in (
            visible_transactions(workspace, user)
            .filter(
                date__year=year,
                date__month=month,
                type=Transaction.TYPE_EXPENSE,
                counts_toward_budget=True,
            )
            .values("category")
            .annotate(spent=Sum("amount"))
        )
    }
    provisions = {
        p.category_id: p.accumulated_amount
        for p in CategoryProvision.objects.filter(category__workspace=workspace)
    }

    cat_ids = set(budgets) | set(spent)
    names = dict(
        Category.objects.filter(id__in=cat_ids).values_list("id", "name")
    )

    rows = []
    for cid in cat_ids:
        budgeted = budgets.get(cid, Decimal("0"))
        used = spent.get(cid, Decimal("0"))
        rows.append(
            {
                "category": str(cid),
                "category_name": names.get(cid),
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
    return {"year": year, "month": month, "rows": rows, "totals": totals}


def monthly_cashflow(workspace, user, months=6, until=None):
    until = (until or timezone.localdate()).replace(day=1)
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
        income = _sum(month_txns.filter(type=Transaction.TYPE_INCOME), "amount")
        expenses = _sum(month_txns.filter(type=Transaction.TYPE_EXPENSE), "amount")
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


def dashboard_summary(workspace, user, today=None):
    today = today or timezone.localdate()
    from apps.email_import.models import EmailImportLog

    this_month = monthly_cashflow(workspace, user, months=1, until=today)[0]
    return {
        "month": this_month,
        "net_worth": net_worth_breakdown(workspace, user)["net"],
        "pending_email_imports": EmailImportLog.objects.filter(
            workspace=workspace, status=EmailImportLog.STATUS_PENDING
        ).count(),
        "top_expense_categories": spending_by_category(
            workspace, user, today.year, today.month
        )[:5],
    }


def close_month(year, month, workspace=None):
    """Crea/actualiza el MonthlySnapshot de cada workspace y hace el rollover."""
    workspaces = [workspace] if workspace is not None else Workspace.objects.all()
    snapshots = []

    for ws in workspaces:
        month_txns = Transaction.objects.filter(
            wallet__workspace=ws, date__year=year, date__month=month
        )
        snapshot, _ = MonthlySnapshot.objects.update_or_create(
            workspace=ws,
            year=year,
            month=month,
            defaults={
                "total_net_worth": net_worth(ws),
                "total_income": _sum(
                    month_txns.filter(type=Transaction.TYPE_INCOME), "amount"
                ),
                "total_expenses": _sum(
                    month_txns.filter(type=Transaction.TYPE_EXPENSE), "amount"
                ),
            },
        )
        snapshots.append(snapshot)
        _rollover_provisions(ws, year, month)

    return snapshots


def _rollover_provisions(workspace, year, month):
    """Suma el sobrante (presupuesto - gasto real) de cada categoría a su provisión."""
    budgets = CategoryBudget.objects.filter(
        workspace=workspace, year=year, month=month
    ).select_related("category")

    for budget in budgets:
        spent = _sum(
            Transaction.objects.filter(
                category=budget.category,
                date__year=year,
                date__month=month,
                counts_toward_budget=True,
            ),
            "amount",
        )
        leftover = budget.amount - spent
        if leftover <= 0:
            continue

        provision, _ = CategoryProvision.objects.get_or_create(category=budget.category)
        CategoryProvision.objects.filter(pk=provision.pk).update(
            accumulated_amount=F("accumulated_amount") + leftover,
            last_updated=timezone.localdate(),
        )
