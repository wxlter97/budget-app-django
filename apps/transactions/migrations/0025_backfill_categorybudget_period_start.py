from datetime import date

from django.db import migrations


def backfill_period_start(apps, schema_editor):
    """Todo `CategoryBudget` existente se creó cuando el único período
    posible era "mes calendario" -- su `period_start` es simplemente el 1°
    del mes/año que ya tenía (ver `apps.common.periods.period_start` para
    el caso general, que a partir de acá cubre los otros cuatro tipos)."""
    CategoryBudget = apps.get_model("transactions", "CategoryBudget")
    for budget in CategoryBudget.objects.all().only("id", "year", "month"):
        budget.period_start = date(budget.year, budget.month, 1)
        budget.save(update_fields=["period_start"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0024_categorybudget_add_period_start"),
    ]

    operations = [
        migrations.RunPython(backfill_period_start, noop),
    ]
