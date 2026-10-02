from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0031_indices_de_consulta'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='rollover_surplus',
            field=models.BooleanField(default=True, verbose_name='acumular sobrante de presupuesto'),
        ),
    ]
