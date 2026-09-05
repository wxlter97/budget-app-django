"""
`Transaction.currency` pasó a derivarse siempre de `wallet.currency` en
`Transaction.save()` (ver el modelo) — antes quedaba en el default "USD"
sin importar la cartera real. Esto corrige las filas existentes para que
coincidan con la cartera real de cada una; sin tasas de cambio ni
multi-moneda de por medio, casi todo el mundo tenía todo en USD igual, así
que en la práctica esto no debería cambiar nada salvo para quien ya tuviera
carteras en otra moneda.

Nota: `.update(currency=F("wallet__currency"))` no sirve acá -- Django no
permite F() que cruce relaciones dentro de un `.update()`. Por eso el
`bulk_update` fila por fila (la cantidad de transacciones de una app
personal no amerita nada más elaborado).
"""
from django.db import migrations


def backfill_currency(apps, schema_editor):
    Transaction = apps.get_model("transactions", "Transaction")
    Wallet = apps.get_model("accounts", "Wallet")

    wallet_currency = dict(Wallet.objects.values_list("id", "currency"))

    to_update = []
    for txn in Transaction.objects.all().only("id", "wallet_id", "currency"):
        real_currency = wallet_currency.get(txn.wallet_id)
        if real_currency and txn.currency != real_currency:
            txn.currency = real_currency
            to_update.append(txn)

    if to_update:
        Transaction.objects.bulk_update(to_update, ["currency"], batch_size=500)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0009_alter_transaction_source"),
    ]

    operations = [
        migrations.RunPython(backfill_currency, noop_reverse),
    ]
