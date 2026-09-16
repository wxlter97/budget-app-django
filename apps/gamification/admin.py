from django.contrib import admin

from .models import Badge, WorkspaceBadge


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "icon")
    search_fields = ("name", "code")


@admin.register(WorkspaceBadge)
class WorkspaceBadgeAdmin(admin.ModelAdmin):
    list_display = ("workspace", "badge", "earned_at")
    list_filter = ("badge",)
    autocomplete_fields = ("workspace", "badge")
    readonly_fields = ("earned_at",)
