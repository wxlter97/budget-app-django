from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0003_transaction_type_transfer_budget_flag"),
        ("accounts", "0004_wallet"),
    ]

    operations = [
        migrations.RenameField("transaction", "account", "wallet"),
        migrations.RenameField("transaction", "to_account", "to_wallet"),
        migrations.RenameField("recurringexpense", "account", "wallet"),
        migrations.RenameField("installmentpurchase", "account", "wallet"),
    ]
