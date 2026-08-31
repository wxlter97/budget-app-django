from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import MonthlySnapshot


@admin.register(MonthlySnapshot)
class MonthlySnapshotAdmin(BaseModelAdmin):
    list_display = (
        "workspace",
        "year",
        "month",
        "total_net_worth",
        "total_income",
        "total_expenses",
    )
    list_filter = ("year", "month")
    search_fields = ("workspace__name",)
    raw_id_fields = ("workspace",)
