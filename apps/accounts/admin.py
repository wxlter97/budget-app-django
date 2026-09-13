from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Wallet, WalletCard


class WalletCardInline(admin.TabularInline):
    model = WalletCard
    extra = 0
    fields = ("last4", "label")


@admin.register(Wallet)
class WalletAdmin(BaseModelAdmin):
    list_display = (
        "name", "workspace", "purpose", "kind", "currency",
        "current_balance", "card_product", "counts_toward_net_worth", "is_default", "is_active",
    )
    readonly_fields = BaseModelAdmin.readonly_fields + ("current_balance",)
    list_filter = ("purpose", "kind", "counts_toward_net_worth", "visibility", "is_active", "currency")
    search_fields = ("name", "workspace__name", "card_last4", "counterparty", "extra_cards__last4")
    raw_id_fields = ("workspace", "owner", "parent", "card_product")
    inlines = [WalletCardInline]
