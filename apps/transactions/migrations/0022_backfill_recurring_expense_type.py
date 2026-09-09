from django.db import migrations


def backfill_type(apps, schema_editor):
    """Toda fila existente se creó vía el `category`/`wallet` de siempre (sin
    `type` propio) -- el tipo real ya lo traía la categoría, así que se
    copia de ahí. El default 'expense' del AddField de la migración anterior
    quedaría mal para las que en realidad eran de una categoría de ingreso."""
    RecurringExpense = apps.get_model("transactions", "RecurringExpense")
    RecurringExpense.objects.filter(category__type="income").update(type="income")
    RecurringExpense.objects.filter(category__type="expense").update(type="expense")


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0021_recurringexpense_to_wallet_recurringexpense_type_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_type, migrations.RunPython.noop),
    ]
