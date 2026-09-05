"""Generación automática de transacciones (gastos recurrentes y cuotas).

Las funciones son idempotentes respecto al estado que llevan los propios
modelos (`RecurringExpense.next_due_date`, `InstallmentPurchase.installments_paid`):
correrlas dos veces el mismo día no duplica nada.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from .models import InstallmentPurchase, RecurringExpense, Transaction


def visible_transactions(workspace, user):
    """
    Transacciones del workspace que puede ver ``user``: las de carteras
    compartidas + las de carteras privadas de las que es owner.
    """
    from apps.accounts.models import Wallet

    return Transaction.objects.filter(wallet__workspace=workspace).filter(
        Q(wallet__visibility=Wallet.VISIBILITY_SHARED) | Q(wallet__owner=user)
    )


def _advance(date, frequency):
    delta = RecurringExpense.FREQUENCY_DELTAS.get(
        frequency, RecurringExpense.FREQUENCY_DELTAS[RecurringExpense.FREQUENCY_MONTHLY]
    )
    return date + relativedelta(**delta)


def generate_recurring_transactions(as_of=None):
    """Crea una Transaction por cada período vencido de cada gasto recurrente activo.

    Idempotente: avanza ``next_due_date`` a medida que genera, así una segunda
    corrida el mismo día no duplica nada.
    """
    as_of = as_of or timezone.localdate()
    created = []

    recurring = (
        RecurringExpense.objects.filter(is_active=True, next_due_date__lte=as_of)
        .select_related("category", "wallet")
    )
    for rec in recurring:
        with db_transaction.atomic():
            due = rec.next_due_date
            while due <= as_of:
                created.append(
                    Transaction.objects.create(
                        wallet=rec.wallet,
                        category=rec.category,
                        amount=rec.amount,
                        description=f"{rec.category.name} (recurrente)",
                        date=due,
                        source=Transaction.SOURCE_RECURRING,
                        is_recurring=True,
                    )
                )
                due = _advance(due, rec.frequency)
            rec.next_due_date = due
            rec.save(update_fields=["next_due_date", "updated_at"])

    return created


def _installment_txn_kwargs(purchase, n, date, *, user=None):
    """Campos de la Transaction para la cuota ``n`` de ``purchase``.

    Tarjeta de crédito (`payment_wallet`): la cuota es una TRANSFERENCIA de la
    cartera de pago hacia la tarjeta. Resto: un GASTO contra `wallet`.
    """
    desc = f"{purchase.description} (cuota {n}/{purchase.installments_total})"
    if purchase.payment_wallet_id:
        return dict(
            type=Transaction.TYPE_TRANSFER,
            wallet=purchase.payment_wallet,
            to_wallet=purchase.wallet,
            category=None,
            amount=purchase.installment_amount,
            description=desc,
            date=date,
            source=Transaction.SOURCE_INSTALLMENT,
            created_by=user,
        )
    return dict(
        wallet=purchase.wallet,
        category=purchase.category,
        amount=purchase.installment_amount,
        description=desc,
        date=date,
        source=Transaction.SOURCE_INSTALLMENT,
        created_by=user,
    )


def post_initial_installment_charge(purchase, *, user=None):
    """Solo compras con tarjeta: registra el cargo del total contra la tarjeta
    el día de la compra (ya debes todo y baja tu crédito disponible)."""
    if not purchase.payment_wallet_id:
        return None
    return Transaction.objects.create(
        wallet=purchase.wallet,
        category=purchase.category,
        amount=purchase.total_amount,
        description=f"{purchase.description} (compra a {purchase.installments_total} cuotas)",
        date=purchase.start_date,
        source=Transaction.SOURCE_INSTALLMENT,
        created_by=user,
    )


def post_due_installments(as_of=None):
    """Registra las cuotas vencidas de cada compra a plazo no terminada."""
    as_of = as_of or timezone.localdate()
    created = []

    for purchase in InstallmentPurchase.objects.select_related(
        "category", "wallet", "payment_wallet"
    ):
        if purchase.installments_paid >= purchase.installments_total:
            continue

        months_elapsed = (
            (as_of.year - purchase.start_date.year) * 12
            + (as_of.month - purchase.start_date.month)
        )
        due_count = months_elapsed + (1 if as_of.day >= purchase.start_date.day else 0)
        due_count = min(max(due_count, 0), purchase.installments_total)

        while purchase.installments_paid < due_count:
            n = purchase.installments_paid + 1
            due_date = purchase.start_date + relativedelta(months=n - 1)
            with db_transaction.atomic():
                created.append(
                    Transaction.objects.create(
                        **_installment_txn_kwargs(purchase, n, due_date)
                    )
                )
                purchase.installments_paid = n
                purchase.save(update_fields=["installments_paid", "updated_at"])

    return created


def post_next_installment(purchase, *, user=None, on_date=None):
    """Registra UNA cuota de `purchase`: crea la Transaction y avanza el contador.

    Devuelve la Transaction creada, o None si la compra ya está completa.
    """
    if purchase.installments_paid >= purchase.installments_total:
        return None
    n = purchase.installments_paid + 1
    date = on_date or purchase.start_date + relativedelta(months=n - 1)
    with db_transaction.atomic():
        txn = Transaction.objects.create(
            **_installment_txn_kwargs(purchase, n, date, user=user)
        )
        purchase.installments_paid = n
        purchase.save(update_fields=["installments_paid", "updated_at"])
    return txn


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
