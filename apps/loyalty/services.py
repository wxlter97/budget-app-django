"""Agregados de lealtad para el reporte del workspace (ver `api.py`)."""
from decimal import Decimal

from django.db.models import Sum

from .models import LoyaltyEarning, LoyaltyProgram


def points_balances(workspace) -> list[dict]:
    """Saldo de puntos acumulado por cartera + programa."""
    rows = (
        LoyaltyEarning.objects.filter(workspace=workspace, kind=LoyaltyProgram.KIND_POINTS)
        .values(
            "transaction__wallet_id",
            "transaction__wallet__name",
            "program_id",
            "program__name",
            "program__point_value",
        )
        .annotate(points=Sum("points"))
        .order_by("transaction__wallet__name")
    )
    result = []
    for row in rows:
        points = row["points"] or Decimal("0")
        point_value = row["program__point_value"]
        result.append(
            {
                "wallet": row["transaction__wallet_id"],
                "wallet_name": row["transaction__wallet__name"],
                "program": row["program_id"],
                "program_name": row["program__name"] or "Puntos",
                "points": points,
                "estimated_value": (points * point_value) if point_value is not None else None,
            }
        )
    return result


def period_totals(workspace, date_after=None, date_before=None) -> list[dict]:
    """Cashback ganado y descuento ahorrado por cartera, en el período dado."""
    qs = LoyaltyEarning.objects.filter(
        workspace=workspace, kind__in=[LoyaltyProgram.KIND_CASHBACK, LoyaltyProgram.KIND_DISCOUNT]
    ).select_related("transaction", "transaction__wallet")
    if date_after:
        qs = qs.filter(transaction__date__gte=date_after)
    if date_before:
        qs = qs.filter(transaction__date__lte=date_before)

    buckets: dict = {}
    for earning in qs:
        wallet = earning.transaction.wallet
        bucket = buckets.setdefault(
            wallet.id,
            {
                "wallet": wallet.id,
                "wallet_name": wallet.name,
                "cashback_earned": Decimal("0"),
                "discount_saved": Decimal("0"),
            },
        )
        if earning.kind == LoyaltyProgram.KIND_CASHBACK:
            bucket["cashback_earned"] += earning.amount or Decimal("0")
        else:
            bucket["discount_saved"] += earning.discount_saved_amount or Decimal("0")
    return sorted(buckets.values(), key=lambda b: b["wallet_name"])


def loyalty_summary(workspace, date_after=None, date_before=None) -> dict:
    return {
        "points_balances": points_balances(workspace),
        "period_totals": period_totals(workspace, date_after, date_before),
    }
