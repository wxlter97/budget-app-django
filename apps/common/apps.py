from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    verbose_name = "Común"

    def ready(self):
        from . import checks  # noqa: F401  (registra los checks de --deploy)
