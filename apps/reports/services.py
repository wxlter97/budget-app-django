"""Cierre de mes: snapshot financiero + rollover de provisiones.

Lo dispara la tarea de Celery Beat el día 1 (para el mes anterior), pero se
puede llamar a mano para cualquier (año, mes).
"""
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from apps.accounts.models import Account, Asset, Debt, Liability
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    Transaction,
)
from apps.workspaces.models import Workspace

from .models import MonthlySnapshot


def _sum(qs, field):
    return qs.aggregate(s=Sum(field))["s"] or Decimal("0")


def net_worth(workspace) -> Decimal:
    accounts = _sum(Account.objects.filter(workspace=workspace), "current_balance")
    assets = _sum(Asset.objects.filter(workspace=workspace), "current_value")
    liabilities = _sum(Liability.objects.filter(workspace=workspace), "remaining_amount")
    debts = Debt.objects.filter(workspace=workspace, is_settled=False)
    owed_to_us = _sum(debts.filter(direction=Debt.DIRECTION_FAVOR), "amount")
    we_owe = _sum(debts.filter(direction=Debt.DIRECTION_CONTRA), "amount")
    return accounts + assets - liabilities + owed_to_us - we_owe


def close_month(year, month, workspace=None):
    """Crea/actualiza el MonthlySnapshot de cada workspace y hace el rollover."""
    workspaces = [workspace] if workspace is not None else Workspace.objects.all()
    snapshots = []

    for ws in workspaces:
        month_txns = Transaction.objects.filter(
            account__workspace=ws, date__year=year, date__month=month
        )
        snapshot, _ = MonthlySnapshot.objects.update_or_create(
            workspace=ws,
            year=year,
            month=month,
            defaults={
                "total_net_worth": net_worth(ws),
                "total_income": _sum(
                    month_txns.filter(category__type=Category.TYPE_INCOME), "amount"
                ),
                "total_expenses": _sum(
                    month_txns.filter(category__type=Category.TYPE_EXPENSE), "amount"
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
                category=budget.category, date__year=year, date__month=month
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
