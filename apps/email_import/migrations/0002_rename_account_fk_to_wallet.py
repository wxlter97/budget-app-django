from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("email_import", "0001_initial"),
        ("accounts", "0004_wallet"),
    ]

    operations = [
        migrations.RenameField("emailimportlog", "account", "wallet"),
    ]
