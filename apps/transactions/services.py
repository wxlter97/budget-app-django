"""Generación automática de transacciones (gastos recurrentes y cuotas).

Las funciones son idempotentes respecto al estado que llevan los propios
modelos (`RecurringExpense.next_due_date`, `InstallmentPurchase.installments_paid`):
correrlas dos veces el mismo día no duplica nada.
"""
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
