from django.db import migrations

# (key, label) -- los módulos con un `require_module_enabled`/`module_enabled`
# real hoy (ver `apps.ai.services.availability_for`, `apps.email_import.api`,
# `apps.transactions.api.import_xlsx`). Se seedean habilitados: crear la fila
# de antemano es sólo para que aparezcan en el admin listos para apagar, no
# hace falta para que el gate funcione (fail-open sin fila, ver el modelo).
MODULE_CATALOG = [
    ("ai", "Funciones de IA (recibos, texto libre)"),
    ("email_import", "Importación automática por correo"),
    ("excel_import", "Importar extractos de Excel"),
]


def seed_flags(apps, schema_editor):
    ModuleFlag = apps.get_model("common", "ModuleFlag")
    for key, label in MODULE_CATALOG:
        ModuleFlag.objects.update_or_create(key=key, defaults={"label": label})


def remove_flags(apps, schema_editor):
    ModuleFlag = apps.get_model("common", "ModuleFlag")
    keys = [key for key, _ in MODULE_CATALOG]
    ModuleFlag.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_flags, remove_flags),
    ]
