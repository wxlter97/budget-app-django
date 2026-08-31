from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Account, Asset, Debt, Liability


@admin.register(Account)
class AccountAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "type", "currency", "current_balance", "visibility", "is_active")
    list_filter = ("type", "visibility", "is_active", "currency")
    search_fields = ("name", "workspace__name", "card_last4")
    raw_id_fields = ("workspace", "owner")


@admin.register(Asset)
class AssetAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "type", "current_value", "visibility")
    list_filter = ("type", "visibility")
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace", "owner")


@admin.register(Liability)
class LiabilityAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "type", "total_amount", "remaining_amount", "due_date")
    list_filter = ("type",)
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace",)


@admin.register(Debt)
class DebtAdmin(BaseModelAdmin):
    list_display = ("person", "workspace", "direction", "amount", "is_settled")
    list_filter = ("direction", "is_settled")
    search_fields = ("person", "description", "workspace__name")
    raw_id_fields = ("workspace",)
