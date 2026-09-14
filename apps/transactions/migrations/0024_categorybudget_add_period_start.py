from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0023_alter_transaction_receipt"),
    ]

    operations = [
        migrations.AddField(
            model_name="categorybudget",
            name="period_start",
            field=models.DateField(null=True),
        ),
    ]
