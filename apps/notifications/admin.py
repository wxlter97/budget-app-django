from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(BaseModelAdmin):
    list_display = ("kind", "user", "workspace", "status", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("user__username", "user__email", "title", "body", "related_object_id")
    raw_id_fields = ("user", "workspace")
    date_hierarchy = "created_at"
