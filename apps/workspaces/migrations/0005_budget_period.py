from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0004_invitation"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspace",
            name="budget_period",
            field=models.CharField(
                choices=[
                    ("daily", "Diario"),
                    ("weekly", "Semanal"),
                    ("biweekly", "Quincenal"),
                    ("monthly", "Mensual"),
                    ("yearly", "Anual"),
                ],
                default="monthly",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="workspace",
            name="budget_period_closed_through",
            field=models.DateField(blank=True, null=True),
        ),
    ]
