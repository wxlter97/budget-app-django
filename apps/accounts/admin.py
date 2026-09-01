from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Wallet


@admin.register(Wallet)
class WalletAdmin(BaseModelAdmin):
    list_display = (
        "name", "workspace", "purpose", "currency",
        "current_balance", "counts_toward_net_worth", "is_default", "is_active",
    )
    readonly_fields = BaseModelAdmin.readonly_fields + ("current_balance",)
    list_filter = ("purpose", "counts_toward_net_worth", "visibility", "is_active", "currency")
    search_fields = ("name", "workspace__name", "card_last4", "counterparty")
    raw_id_fields = ("workspace", "owner", "parent")
