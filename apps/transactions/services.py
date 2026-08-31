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
    Transacciones del workspace que puede ver ``user``: las de cuentas
    compartidas + las de cuentas privadas de las que es owner.
    """
    from apps.accounts.models import Account

    return Transaction.objects.filter(account__workspace=workspace).filter(
        Q(account__visibility=Account.VISIBILITY_SHARED) | Q(account__owner=user)
    )


def _advance(date, frequency):
    if frequency == RecurringExpense.FREQUENCY_YEARLY:
        return date + relativedelta(years=1)
    return date + relativedelta(months=1)


def generate_due_recurring_expenses(as_of=None):
    """Crea una Transaction por cada período vencido de cada gasto recurrente activo."""
    as_of = as_of or timezone.localdate()
    created = []

    recurring = (
        RecurringExpense.objects.filter(is_active=True, next_due_date__lte=as_of)
        .select_related("category", "account")
    )
    for rec in recurring:
        with db_transaction.atomic():
            due = rec.next_due_date
            while due <= as_of:
                created.append(
                    Transaction.objects.create(
                        account=rec.account,
                        category=rec.category,
                        amount=rec.amount,
                        description=f"{rec.category.name} (recurrente)",
                        date=due,
                        source=Transaction.SOURCE_MANUAL,
                        is_recurring=True,
                    )
                )
                due = _advance(due, rec.frequency)
            rec.next_due_date = due
            rec.save(update_fields=["next_due_date", "updated_at"])

    return created


def post_due_installments(as_of=None):
    """Registra las cuotas vencidas de cada compra a plazo no terminada."""
    as_of = as_of or timezone.localdate()
    created = []

    for purchase in InstallmentPurchase.objects.select_related("category", "account"):
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
                        account=purchase.account,
                        category=purchase.category,
                        amount=purchase.installment_amount,
                        description=f"{purchase.description} (cuota {n}/{purchase.installments_total})",
                        date=due_date,
                        source=Transaction.SOURCE_MANUAL,
                    )
                )
                purchase.installments_paid = n
                purchase.save(update_fields=["installments_paid", "updated_at"])

    return created
