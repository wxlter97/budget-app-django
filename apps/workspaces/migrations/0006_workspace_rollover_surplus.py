from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0005_budget_period'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspace',
            name='rollover_surplus',
            field=models.BooleanField(default=True),
        ),
    ]
