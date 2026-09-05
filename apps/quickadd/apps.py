from django.apps import AppConfig


class QuickaddConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.quickadd"
    verbose_name = "Alta rápida (tokens personales / Atajos)"

    def ready(self):
        from . import schema  # noqa: F401  (registra el esquema OpenAPI del auth)
