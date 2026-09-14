from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0025_backfill_categorybudget_period_start"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="categorybudget",
            name="unique_budget_per_category_month",
        ),
        migrations.AlterField(
            model_name="categorybudget",
            name="period_start",
            field=models.DateField(),
        ),
        migrations.RemoveField(
            model_name="categorybudget",
            name="month",
        ),
        migrations.RemoveField(
            model_name="categorybudget",
            name="year",
        ),
        migrations.AddConstraint(
            model_name="categorybudget",
            constraint=models.UniqueConstraint(
                fields=("category", "period_start"), name="unique_budget_per_category_period"
            ),
        ),
    ]
