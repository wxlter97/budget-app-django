import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0006_workspace_rollover_surplus'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspace',
            name='week_start_day',
            field=models.PositiveSmallIntegerField(
                default=0, validators=[django.core.validators.MaxValueValidator(6)]
            ),
        ),
    ]
