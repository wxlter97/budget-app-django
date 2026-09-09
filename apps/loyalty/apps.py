from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.loyalty"
    verbose_name = "Lealtad"

    def ready(self):
        from . import signals  # noqa: F401
