"""Generación automática de transacciones (gastos recurrentes) y aritmética
de compras a plazo.

`generate_recurring_transactions` es idempotente respecto a
`RecurringExpense.next_due_date`: correrla dos veces el mismo día no duplica
nada. Las compras a plazo (`InstallmentPurchase`) ya no generan
transacciones por cuota -- `installment_amounts` solo calcula montos para el
estado de cuenta (ver `apps.accounts.services.installment_status`).
"""
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
from django.db.models import Count, Q
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from .models import Category, RecurringExpense, Transaction


def guess_category_by_merchant(*, workspace, txn_type, merchant):
    """
    Adivina la ``Category`` más probable para un comercio, mirando cómo se
    categorizó ese mismo comercio antes: la categoría más frecuente entre
    transacciones pasadas del workspace cuya descripción lo contenga.
    ``None`` si no hay comercio o no hay historial suficiente.

    Usado por el alta rápida de Apple Shortcuts (``apps.quickadd``) y por la
    confirmación de importación de correos bancarios (``apps.email_import``)
    -- mismo comercio, misma forma de adivinar en los dos lugares.
    """
    if not merchant:
        return None

    assignable = Category.objects.filter(workspace=workspace, type=txn_type, parent__isnull=False)

    best = (
        Transaction.objects.filter(
            wallet__workspace=workspace,
            type=txn_type,
            category__isnull=False,
            description__icontains=merchant,
        )
        .values("category")
        .annotate(n=Count("category"))
        .order_by("-n")
        .first()
    )
    if not best:
        return None
    return assignable.filter(pk=best["category"]).first()


def visible_transactions(workspace, user):
    """
    Transacciones del workspace que puede ver ``user``: las de carteras
    compartidas + las de carteras privadas de las que es owner.
    """
    from apps.accounts.models import Wallet

    return Transaction.objects.filter(wallet__workspace=workspace).filter(
        Q(wallet__visibility=Wallet.VISIBILITY_SHARED) | Q(wallet__owner=user)
    )


# ---------------------------------------------------------------------------
# Categorías por defecto (grupo → categoría, estilo Buddy, en es)
# ---------------------------------------------------------------------------
# grupo: (nombre, icono, color, [ (categoría, icono, color), ... ])
DEFAULT_EXPENSE_CATEGORY_GROUPS = [
    ("Vivienda", "🏠", "#F59E0B", [
        ("Alquiler / Préstamo", "🏦", "#F59E0B"),
        ("Internet", "📶", "#F59E0B"),
        ("Electricidad", "⚡", "#F59E0B"),
        ("Agua", "💧", "#F59E0B"),
        ("Teléfono", "📱", "#F59E0B"),
        ("Mantenimiento", "🔧", "#F59E0B"),
    ]),
    ("Comida", "🍽", "#3B82F6", [
        ("Comida", "🍽", "#3B82F6"),
        ("Supermercado", "🛒", "#3B82F6"),
        ("Restaurantes", "🍔", "#3B82F6"),
    ]),
    ("Transporte", "🚗", "#8B5CF6", [
        ("Gasolina", "⛽", "#8B5CF6"),
        ("Transporte público", "🚌", "#8B5CF6"),
        ("Parking", "🅿️", "#8B5CF6"),
        ("Costes de vehículo", "🚗", "#8B5CF6"),
    ]),
    ("Estilo de vida", "✨", "#EC4899", [
        ("Suscripciones", "🔁", "#EC4899"),
        ("Entretenimiento", "🎬", "#EC4899"),
        ("Ropa", "👕", "#EC4899"),
        ("Gimnasio", "🏋️", "#EC4899"),
        ("Bienestar", "❤️", "#EC4899"),
        ("Regalos", "🎁", "#EC4899"),
        ("Hobby", "🎨", "#EC4899"),
    ]),
    ("Salud", "🏥", "#EF4444", [
        ("Salud", "🏥", "#EF4444"),
        ("Farmacia", "💊", "#EF4444"),
    ]),
    ("Educación", "📚", "#6366F1", [
        ("Educación", "📚", "#6366F1"),
    ]),
    ("Ahorro", "🐷", "#14B8A6", [
        ("Ahorro", "🐷", "#14B8A6"),
    ]),
    ("Otros", "📦", "#94A3B8", [
        ("Impuestos", "🧾", "#94A3B8"),
        ("Comisiones", "🏛️", "#94A3B8"),
        ("Otros gastos", "💸", "#94A3B8"),
    ]),
]

DEFAULT_INCOME_CATEGORY_GROUPS = [
    ("Ingresos", "💰", "#22C55E", [
        ("Sueldo", "💼", "#22C55E"),
        ("Freelance", "🧑‍💻", "#22C55E"),
        ("Inversiones", "📈", "#22C55E"),
        ("Reembolsos", "↩️", "#22C55E"),
        ("Regalos", "🎁", "#22C55E"),
        ("Otros ingresos", "💰", "#22C55E"),
    ]),
]


def seed_default_categories(workspace) -> int:
    """
    Crea el set de categorías por defecto (grupos → categorías) en
    ``workspace`` -- idempotente (``get_or_create`` por workspace + nombre +
    tipo), así que correrlo sobre un workspace que ya tiene categorías sólo
    agrega lo que falte, nunca duplica ni pisa lo que el usuario ya armó.

    La llaman tanto ``WorkspaceViewSet.perform_create`` (todo workspace
    nuevo arranca con esto, nunca en blanco) como el comando de management
    ``seed_categories`` (para aplicarlo a mano a workspaces que ya existían
    de antes). Devuelve cuántas categorías nuevas creó.
    """
    created = 0
    order = 0
    for cat_type, groups in (
        (Category.TYPE_EXPENSE, DEFAULT_EXPENSE_CATEGORY_GROUPS),
        (Category.TYPE_INCOME, DEFAULT_INCOME_CATEGORY_GROUPS),
    ):
        for gname, gicon, gcolor, children in groups:
            group, made = Category.objects.get_or_create(
                workspace=workspace, name=gname, type=cat_type, parent=None,
                defaults={"icon": gicon, "color": gcolor, "sort_order": order},
            )
            order += 1
            created += int(made)
            for cname, cicon, ccolor in children:
                _, made = Category.objects.get_or_create(
                    workspace=workspace, name=cname, type=cat_type,
                    defaults={"icon": cicon, "color": ccolor, "parent": group, "sort_order": order},
                )
                order += 1
                created += int(made)
    return created


def _advance(date, frequency):
    delta = RecurringExpense.FREQUENCY_DELTAS.get(
        frequency, RecurringExpense.FREQUENCY_DELTAS[RecurringExpense.FREQUENCY_MONTHLY]
    )
    return date + relativedelta(**delta)


def _recurring_description(rec) -> str:
    if rec.name:
        return f"{rec.name} (recurrente)"
    if rec.category:
        return f"{rec.category.name} (recurrente)"
    return f"Transferencia a {rec.to_wallet.name} (recurrente)"


def generate_recurring_transactions(as_of=None):
    """Crea una Transaction por cada período vencido de cada gasto recurrente activo.

    Idempotente: avanza ``next_due_date`` a medida que genera, así una segunda
    corrida el mismo día no duplica nada. Puede generar income/expense
    (contra `category`) o transfer (contra `to_wallet`, p. ej. un aporte
    automático a una cartera de ahorro con meta) según `rec.type`.
    """
    as_of = as_of or timezone.localdate()
    created = []

    recurring = (
        RecurringExpense.objects.filter(is_active=True, next_due_date__lte=as_of)
        .select_related("category", "wallet", "to_wallet")
    )
    for rec in recurring:
        is_transfer = rec.type == RecurringExpense.TYPE_TRANSFER
        with db_transaction.atomic():
            due = rec.next_due_date
            while due <= as_of:
                created.append(
                    Transaction.objects.create(
                        type=rec.type,
                        wallet=rec.wallet,
                        to_wallet=rec.to_wallet if is_transfer else None,
                        category=None if is_transfer else rec.category,
                        amount=rec.amount,
                        description=_recurring_description(rec),
                        date=due,
                        source=Transaction.SOURCE_RECURRING,
                        is_recurring=True,
                    )
                )
                due = _advance(due, rec.frequency)
            rec.next_due_date = due
            rec.save(update_fields=["next_due_date", "updated_at"])

    return created


def installment_amounts(total_amount: Decimal, installments_total: int) -> list[Decimal]:
    """Reparte `total_amount` en `installments_total` cuotas para el cálculo
    del estado de cuenta: cada cuota, menos la última, se redondea hacia
    arriba al centavo (p. ej. USD 12.244 -> USD 12.25); la última es lo que
    sobra, para que la suma cierre exacto con `total_amount` sin importar el
    redondeo de las anteriores."""
    total_cents = int((total_amount * 100).to_integral_value(rounding=ROUND_CEILING))
    per_cents = -(-total_cents // installments_total)  # división entera hacia arriba
    amounts = [Decimal(per_cents) / 100] * (installments_total - 1)
    last = total_amount - sum(amounts, Decimal("0"))
    amounts.append(last)
    return amounts


# ---------------------------------------------------------------------------
# Detección de recurrentes ("esto se repite hace 3 meses, ¿lo marco como
# recurrente?", Fase 2 del roadmap)
# ---------------------------------------------------------------------------
def _approx_amount(amount) -> Decimal:
    """Redondea al entero más cercano -- para que una sugerencia descartada
    siga reconociéndose aunque el monto varíe unos centavos de mes a mes."""
    return Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def detect_recurring_candidates(workspace, user, months=4, min_occurrences=3):
    """Transacciones que se repiten mes a mes en la misma categoría+cartera,
    con un monto parecido, y que todavía no están marcadas como recurrentes.

    Reglas, a propósito conservadoras para no inundar de falsos positivos una
    categoría con mucho movimiento como "Comida":

    - Como mucho UNA transacción por mes en esa categoría+cartera -- si hay
      más de una en algún mes, no es "una suscripción", es gasto normal y se
      descarta el grupo entero.
    - Aparece en al menos ``min_occurrences`` de los últimos ``months`` meses.
    - El monto no varía más de 15% (o $2, lo que sea mayor) entre la
      ocurrencia más chica y la más grande.
    - No hay ya un `RecurringExpense` activo para esa categoría+cartera.
    - El usuario no la descartó antes (`RecurringSuggestionDismissal`).
    """
    from .models import RecurringSuggestionDismissal

    until = timezone.localdate().replace(day=1)
    since = until - relativedelta(months=months - 1)

    txns = (
        visible_transactions(workspace, user)
        .filter(date__gte=since, type__in=[Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE])
        .exclude(source__in=[Transaction.SOURCE_RECURRING, Transaction.SOURCE_INSTALLMENT])
        .exclude(category__isnull=True)
        .select_related("category", "wallet")
        .order_by("date")
    )

    groups: dict = {}
    for t in txns:
        key = (t.type, t.category_id, t.wallet_id)
        g = groups.setdefault(
            key,
            {
                "type": t.type,
                "category_id": t.category_id,
                "category_name": t.category.name,
                "wallet_id": t.wallet_id,
                "wallet_name": t.wallet.name,
                "by_month": {},
            },
        )
        g["by_month"].setdefault((t.date.year, t.date.month), []).append(t)

    already_recurring = set(
        RecurringExpense.objects.filter(workspace=workspace, is_active=True).values_list(
            "category_id", "wallet_id"
        )
    )
    dismissed = {
        (d.category_id, d.wallet_id, d.approx_amount)
        for d in RecurringSuggestionDismissal.objects.filter(workspace=workspace)
    }

    candidates = []
    for g in groups.values():
        by_month = g["by_month"]
        if any(len(v) > 1 for v in by_month.values()):
            continue
        if len(by_month) < min_occurrences:
            continue
        if (g["category_id"], g["wallet_id"]) in already_recurring:
            continue

        amounts = [v[0].amount for v in by_month.values()]
        avg = sum(amounts) / len(amounts)
        tolerance = max(Decimal("2"), avg * Decimal("0.15"))
        if max(amounts) - min(amounts) > tolerance:
            continue

        approx = _approx_amount(avg)
        if (g["category_id"], g["wallet_id"], approx) in dismissed:
            continue

        last_txn = max((v[0] for v in by_month.values()), key=lambda t: t.date)
        candidates.append(
            {
                "type": g["type"],
                "category": g["category_id"],
                "category_name": g["category_name"],
                "wallet": g["wallet_id"],
                "wallet_name": g["wallet_name"],
                "suggested_amount": avg.quantize(Decimal("0.01")),
                "occurrences": len(by_month),
                "last_date": last_txn.date,
                "suggested_next_due_date": last_txn.date + relativedelta(months=1),
            }
        )

    candidates.sort(key=lambda c: -c["occurrences"])
    return candidates


def dismiss_recurring_suggestion(workspace, category, wallet, amount):
    from .models import RecurringSuggestionDismissal

    RecurringSuggestionDismissal.objects.get_or_create(
        workspace=workspace, category=category, wallet=wallet,
        approx_amount=_approx_amount(amount),
    )
