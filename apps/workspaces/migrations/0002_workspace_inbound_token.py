import secrets

from django.db import migrations, models

import apps.workspaces.models


def populate_tokens(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    for workspace in Workspace.objects.all():
        workspace.inbound_token = secrets.token_urlsafe(9)
        workspace.save(update_fields=["inbound_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0001_initial"),
    ]

    operations = [
        # 1. agregar el campo sin unique (mismo valor temporal en todas las filas)
        migrations.AddField(
            model_name="workspace",
            name="inbound_token",
            field=models.CharField(
                default=apps.workspaces.models.generate_inbound_token,
                editable=False,
                max_length=32,
            ),
            preserve_default=False,
        ),
        # 2. darle un valor único a cada workspace existente
        migrations.RunPython(populate_tokens, migrations.RunPython.noop),
        # 3. ahora sí, imponer unicidad
        migrations.AlterField(
            model_name="workspace",
            name="inbound_token",
            field=models.CharField(
                default=apps.workspaces.models.generate_inbound_token,
                editable=False,
                max_length=32,
                unique=True,
            ),
        ),
    ]
