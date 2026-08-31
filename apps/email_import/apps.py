from django.apps import AppConfig


class EmailImportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.email_import"
    verbose_name = "Importación por correo bancario"

    def ready(self):
        from . import bank_parsers  # noqa: F401  (registra los parsers)
