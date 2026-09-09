from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import TwoFactorAuth, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "first_name", "last_name")
    ordering = ("username",)


@admin.register(TwoFactorAuth)
class TwoFactorAuthAdmin(admin.ModelAdmin):
    # Nunca `secret`/`backup_codes` en list_display ni search -- son
    # credenciales, no algo para ojear de pasada en el listado.
    list_display = ("user", "enabled", "confirmed_at", "created_at")
    list_filter = ("enabled",)
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("secret", "backup_codes", "created_at", "updated_at", "confirmed_at")
