from django.db import migrations, models
import django.db.models.deletion


def backfill_type(apps, schema_editor):
    """Rellena `type` de las transacciones existentes desde `category.type`."""
    Transaction = apps.get_model("transactions", "Transaction")
    for txn in Transaction.objects.select_related("category").all():
        new_type = txn.category.type if txn.category_id else "expense"
        if txn.type != new_type:
            txn.type = new_type
            txn.save(update_fields=["type"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_account_is_default"),
        ("transactions", "0002_alter_transaction_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="type",
            field=models.CharField(
                choices=[
                    ("income", "Ingreso"),
                    ("expense", "Gasto"),
                    ("transfer", "Transferencia"),
                ],
                default="expense",
                max_length=10,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_type, migrations.RunPython.noop),
        migrations.AddField(
            model_name="transaction",
            name="to_account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="incoming_transfers",
                to="accounts.account",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="counts_toward_budget",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transactions",
                to="transactions.category",
            ),
        ),
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.CheckConstraint(
                condition=models.Q(type__in=["income", "expense", "transfer"]),
                name="transaction_type_valid",
            ),
        ),
    ]
