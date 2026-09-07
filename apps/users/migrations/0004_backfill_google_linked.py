from django.db import migrations


def backfill_google_linked(apps, schema_editor):
    """Las cuentas creadas antes de este campo por "Continuar con Google" no
    tienen contraseña utilizable (ver `GoogleLoginView.post`) -- es la única
    forma de distinguirlas retroactivamente. Sin esto, cualquiera que ya
    entraba por Google se queda afuera hasta volver a vincularla a mano."""
    User = apps.get_model("users", "User")
    User.objects.filter(password__startswith="!").update(google_linked=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0003_user_google_linked"),
    ]

    operations = [
        migrations.RunPython(backfill_google_linked, noop),
    ]
