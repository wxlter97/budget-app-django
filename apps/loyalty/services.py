"""Agregados de lealtad para el reporte del workspace (ver `api.py`) y el
reconocimiento de comercios en la descripción de una transacción."""
import re
import unicodedata
from decimal import Decimal

from django.db.models import Sum

from .models import LoyaltyEarning, LoyaltyProgram, Merchant


def normalize_text(text: str) -> str:
    """Minúsculas, sin tildes y con todo lo que no sea letra o número como un
    espacio: "McDonald's — Metrocentro" -> "mcdonald s metrocentro". La misma
    regla vive en el front (`lib/loyaltyRate.ts`); si se cambia una, la otra."""
    stripped = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def match_merchant(description: str, merchants=None):
    """El comercio (`Merchant`) que aparece en la descripción, o `None`.

    Un alias cuenta sólo como palabra(s) completa(s) (así "mc" no dispara con
    "mcdonalds" ni "uno" con "alguno"); si varios coinciden gana el alias más
    largo ("uber eats" antes que "uber"). `merchants` permite pasar la lista ya
    cargada cuando se reconocen muchas descripciones seguidas."""
    text = f" {normalize_text(description)} "
    if not text.strip():
        return None
    best, best_len = None, 0
    for merchant in merchants if merchants is not None else Merchant.objects.all():
        for alias in merchant.alias_list:
            needle = normalize_text(alias)
            if len(needle) > best_len and f" {needle} " in text:
                best, best_len = merchant, len(needle)
    return best


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
