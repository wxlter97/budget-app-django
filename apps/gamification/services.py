"""Racha de días sin gasto fuera de presupuesto, fines de semana sin gastos,
% de ahorro mensual y badges. Todo calculado al vuelo desde `Transaction`
(ver docstring de `models.py`) salvo qué badges ya se otorgaron.

Un "no-spend day" es un día sin ninguna transacción de gasto que cuente
para el presupuesto (`type=expense, counts_toward_budget=True`) -- una
transferencia a una cartera de ahorro, o un gasto marcado explícitamente
fuera de presupuesto, no rompen la racha."""
import calendar
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, When
from django.utils import timezone

from .models import Badge, WorkspaceBadge

_MONEY = DecimalField(max_digits=14, decimal_places=2)

BADGE_STREAK_7 = "streak_7"
BADGE_STREAK_30 = "streak_30"
BADGE_STREAK_100 = "streak_100"
BADGE_FIRST_NO_SPEND_WEEKEND = "first_no_spend_weekend"
BADGE_SAVINGS_10PCT = "savings_10pct"
BADGE_SAVINGS_20PCT = "savings_20pct"

BADGE_CATALOG = [
    (BADGE_STREAK_7, "Racha de 7 días", "7 días seguidos sin gastos fuera de presupuesto.", "flame"),
    (BADGE_STREAK_30, "Racha de 30 días", "Un mes entero sin gastos fuera de presupuesto.", "flame"),
    (BADGE_STREAK_100, "Racha de 100 días", "100 días seguidos sin gastos fuera de presupuesto.", "flame"),
    (
        BADGE_FIRST_NO_SPEND_WEEKEND,
        "Fin de semana sin gastos",
        "Un sábado y domingo seguidos sin ningún gasto que cuente para el presupuesto.",
        "calendar",
    ),
    (BADGE_SAVINGS_10PCT, "Ahorrador 10%", "Ahorraste al menos 10% de tus ingresos en un mes.", "trending"),
    (BADGE_SAVINGS_20PCT, "Ahorrador 20%", "Ahorraste al menos 20% de tus ingresos en un mes.", "trending"),
]


def _spend_days(workspace, start: date_cls, end: date_cls) -> set:
    """Fechas entre `start` y `end` (inclusive) con al menos un gasto que
    cuenta para el presupuesto."""
    from apps.transactions.models import Transaction

    if start > end:
        return set()
    return set(
        Transaction.objects.filter(
            wallet__workspace=workspace,
            type=Transaction.TYPE_EXPENSE,
            counts_toward_budget=True,
            date__gte=start,
            date__lte=end,
        ).values_list("date", flat=True)
    )


def is_no_spend_day(workspace, day: date_cls) -> bool:
    return day not in _spend_days(workspace, day, day)


def current_streak(workspace, as_of: date_cls | None = None) -> int:
    """Días consecutivos hasta `as_of` (hoy por defecto), yendo hacia atrás,
    sin ningún gasto que cuente para el presupuesto. No cuenta más atrás de
    que se creó el workspace."""
    as_of = as_of or timezone.localdate()
    earliest = workspace.created_at.date()
    if as_of < earliest:
        return 0
    spend_days = _spend_days(workspace, earliest, as_of)
    streak = 0
    day = as_of
    while day >= earliest and day not in spend_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def longest_streak(workspace) -> int:
    """La racha más larga que haya tenido este workspace en toda su
    historia (no solo la actual)."""
    today = timezone.localdate()
    earliest = workspace.created_at.date()
    if earliest > today:
        return 0
    spend_days = _spend_days(workspace, earliest, today)
    longest = current = 0
    day = earliest
    while day <= today:
        if day in spend_days:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
        day += timedelta(days=1)
    return longest


def no_spend_weekends_count(workspace) -> int:
    """Cuántos fines de semana (sábado + domingo consecutivos, ambos ya
    ocurridos) tuvieron cero gastos que cuenten para el presupuesto."""
    today = timezone.localdate()
    earliest = workspace.created_at.date()
    if earliest > today:
        return 0
    spend_days = _spend_days(workspace, earliest, today)
    count = 0
    day = earliest
    while day <= today:
        if day.weekday() == 5:  # sábado
            sunday = day + timedelta(days=1)
            if sunday <= today and day not in spend_days and sunday not in spend_days:
                count += 1
        day += timedelta(days=1)
    return count


def monthly_savings_percentage(workspace, year: int, month: int) -> Decimal | None:
    """(ingresos - gastos) / ingresos * 100 del mes dado, sobre TODO el
    movimiento real (a diferencia de la racha, acá sí cuentan los gastos
    marcados fuera de presupuesto -- es el ahorro de caja real, no el
    envelope budgeting). `None` sin ingresos ese mes (el % no tiene sentido)."""
    from apps.transactions.models import Transaction

    first = date_cls(year, month, 1)
    last = date_cls(year, month, calendar.monthrange(year, month)[1])
    totals = Transaction.objects.filter(
        wallet__workspace=workspace,
        date__gte=first,
        date__lte=last,
        type__in=[Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE],
    ).aggregate(
        income=Sum(
            Case(When(type=Transaction.TYPE_INCOME, then=F("amount")), default=Decimal("0"), output_field=_MONEY)
        ),
        expense=Sum(
            Case(When(type=Transaction.TYPE_EXPENSE, then=F("amount")), default=Decimal("0"), output_field=_MONEY)
        ),
    )
    income = totals["income"] or Decimal("0")
    expense = totals["expense"] or Decimal("0")
    if income <= 0:
        return None
    return ((income - expense) / income * 100).quantize(Decimal("0.1"))


def evaluate_and_award_badges(workspace, *, longest: int, weekends: int, savings_pct) -> list:
    """Otorga (idempotente, vía `unique_badge_per_workspace`) cualquier
    badge nuevo que ya se haya ganado según las estadísticas actuales.
    Perezoso: se llama en cada `summary()`, no hay tarea periódica."""
    earned_codes = set()
    if longest >= 7:
        earned_codes.add(BADGE_STREAK_7)
    if longest >= 30:
        earned_codes.add(BADGE_STREAK_30)
    if longest >= 100:
        earned_codes.add(BADGE_STREAK_100)
    if weekends >= 1:
        earned_codes.add(BADGE_FIRST_NO_SPEND_WEEKEND)
    if savings_pct is not None and savings_pct >= 10:
        earned_codes.add(BADGE_SAVINGS_10PCT)
    if savings_pct is not None and savings_pct >= 20:
        earned_codes.add(BADGE_SAVINGS_20PCT)

    if not earned_codes:
        return []

    already = set(
        WorkspaceBadge.objects.filter(workspace=workspace, badge__code__in=earned_codes).values_list(
            "badge__code", flat=True
        )
    )
    new_codes = earned_codes - already
    if not new_codes:
        return []

    WorkspaceBadge.objects.bulk_create(
        (WorkspaceBadge(workspace=workspace, badge=b) for b in Badge.objects.filter(code__in=new_codes)),
        ignore_conflicts=True,
    )
    return list(WorkspaceBadge.objects.filter(workspace=workspace, badge__code__in=new_codes))


def summary(workspace) -> dict:
    today = timezone.localdate()
    current = current_streak(workspace, today)
    longest = longest_streak(workspace)
    weekends = no_spend_weekends_count(workspace)
    savings_pct = monthly_savings_percentage(workspace, today.year, today.month)

    evaluate_and_award_badges(workspace, longest=longest, weekends=weekends, savings_pct=savings_pct)

    earned_codes = set(
        WorkspaceBadge.objects.filter(workspace=workspace).values_list("badge__code", flat=True)
    )
    badges = [
        {
            "code": b.code,
            "name": b.name,
            "description": b.description,
            "icon": b.icon,
            "earned": b.code in earned_codes,
        }
        for b in Badge.objects.all()
    ]

    return {
        "current_streak": current,
        "longest_streak": longest,
        "no_spend_weekends": weekends,
        "monthly_savings_pct": savings_pct,
        "badges": badges,
    }
