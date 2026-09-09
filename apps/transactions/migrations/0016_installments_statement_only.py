"""Rediseño de "compras a plazo": ya no generan una Transaction por cuota
(botón/cron) ni guardan `payment_wallet`/`installment_amount`/
`installments_paid` -- ahora es una única Transaction al crear la compra
(el total, contra la tarjeta de origen) y las cuotas son puro cálculo sobre
los cortes de esa tarjeta (ver `apps.accounts.services.installment_status`).

No hay compras a plazo reales en producción todavía, así que en vez de
migrar los datos existentes (fila por fila, reconstruyendo cuál transacción
corresponde a cuál cuota) se resetea limpio: se borran las
`InstallmentPurchase` y las `Transaction` que hayan generado
(`source=installment`), y se recalcula el saldo de las carteras que
pudieran haber quedado afectadas por ese borrado.

El `RemoveField` de los campos viejos va en la migración siguiente
(0017) a propósito, en vez de acá mismo: Postgres no permite un
`ALTER TABLE` sobre `transactions_installmentpurchase` en la MISMA
transacción en la que se acaba de hacer un DELETE sobre esa tabla (el
DELETE deja eventos de trigger de FK pendientes -- `ObjectInUse: cannot
ALTER TABLE ... because it has pending trigger events`). Cada migración
es su propia transacción, así que separarlas evita el error.
"""
from decimal import Decimal

from django.db import migrations


def reset_installments(apps, schema_editor):
    Transaction = apps.get_model("transactions", "Transaction")
    InstallmentPurchase = apps.get_model("transactions", "InstallmentPurchase")
    Wallet = apps.get_model("accounts", "Wallet")

    installment_txns = Transaction.objects.filter(source="installment")
    affected_wallet_ids = set(
        installment_txns.values_list("wallet_id", flat=True)
    ) | set(
        installment_txns.exclude(to_wallet__isnull=True).values_list(
            "to_wallet_id", flat=True
        )
    )

    installment_txns.delete()
    InstallmentPurchase.objects.all().delete()

    # El borrado directo de arriba no dispara los signals que mantienen
    # `current_balance` -- se recalcula desde cero para cada cartera tocada.
    for wallet in Wallet.objects.filter(id__in=affected_wallet_ids):
        total = Decimal("0")
        for t in Transaction.objects.filter(wallet=wallet, is_deleted=False):
            if t.type == "income":
                total += t.amount
            else:  # expense y transfer saliente restan por igual
                total -= t.amount
        for t in Transaction.objects.filter(
            to_wallet=wallet, type="transfer", is_deleted=False
        ):
            total += t.amount
        wallet.current_balance = wallet.opening_balance + total
        wallet.save(update_fields=["current_balance"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_wallet_bank_schema"),
        ("transactions", "0015_transaction_installment_purchase"),
    ]

    operations = [
        migrations.RunPython(reset_installments, noop_reverse),
    ]
