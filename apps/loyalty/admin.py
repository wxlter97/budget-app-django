from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import Bank, CardProduct, CategoryType, LoyaltyCategoryRate, LoyaltyEarning, LoyaltyProgram


class LoyaltyCategoryRateInline(admin.TabularInline):
    model = LoyaltyCategoryRate
    extra = 1


class LoyaltyProgramInline(admin.TabularInline):
    model = LoyaltyProgram
    extra = 0
    show_change_link = True


@admin.register(Bank)
class BankAdmin(BaseModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(CategoryType)
class CategoryTypeAdmin(BaseModelAdmin):
    list_display = ("name", "slug", "icon")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(CardProduct)
class CardProductAdmin(BaseModelAdmin):
    list_display = ("name", "bank", "network")
    list_filter = ("bank", "network")
    search_fields = ("name", "bank__name")
    inlines = [LoyaltyProgramInline]


@admin.register(LoyaltyProgram)
class LoyaltyProgramAdmin(BaseModelAdmin):
    list_display = ("__str__", "card_product", "kind", "default_rate", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "card_product__name", "card_product__bank__name")
    raw_id_fields = ("card_product",)
    inlines = [LoyaltyCategoryRateInline]


@admin.register(LoyaltyEarning)
class LoyaltyEarningAdmin(BaseModelAdmin):
    """Puntos/cashback los genera sola la señal de Transaction; descuento lo
    genera el cliente al aplicarlo -- este admin es sólo de consulta."""

    list_display = ("transaction", "program", "kind", "points", "amount", "original_amount", "created_at")
    list_filter = ("kind",)
    search_fields = ("transaction__description", "program__name")
    raw_id_fields = ("workspace", "transaction", "program")
    date_hierarchy = "created_at"
