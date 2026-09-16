from django.db import migrations

from apps.gamification.services import BADGE_CATALOG


def seed_badges(apps, schema_editor):
    Badge = apps.get_model("gamification", "Badge")
    for code, name, description, icon in BADGE_CATALOG:
        Badge.objects.update_or_create(
            code=code, defaults={"name": name, "description": description, "icon": icon}
        )


def remove_badges(apps, schema_editor):
    Badge = apps.get_model("gamification", "Badge")
    codes = [code for code, *_ in BADGE_CATALOG]
    Badge.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gamification", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_badges, remove_badges),
    ]
