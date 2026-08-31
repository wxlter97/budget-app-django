from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Membership, Workspace


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    raw_id_fields = ("user",)
    readonly_fields = ("joined_at",)


@admin.register(Workspace)
class WorkspaceAdmin(BaseModelAdmin):
    list_display = ("name", "id", "created_at")
    search_fields = ("name", "id")
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(BaseModelAdmin):
    list_display = ("user", "workspace", "role", "joined_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "workspace__name")
    raw_id_fields = ("user", "workspace")
    readonly_fields = BaseModelAdmin.readonly_fields + ("joined_at",)
