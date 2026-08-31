from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import ReserveFund, SavingsGoal


@admin.register(SavingsGoal)
class SavingsGoalAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "current_amount", "target_amount", "target_date")
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace",)
    date_hierarchy = "target_date"


@admin.register(ReserveFund)
class ReserveFundAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "current_amount", "monthly_contribution")
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace",)
