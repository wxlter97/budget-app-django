from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_account_opening_balance"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                fields=("workspace",),
                condition=models.Q(is_default=True, is_deleted=False),
                name="one_default_account_per_workspace",
            ),
        ),
    ]
