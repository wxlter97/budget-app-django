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
    fields = ("kind", "name", "default_rate", "point_value", "is_active", "rates_count")
    readonly_fields = ("rates_count",)

    @admin.display(description="Tasas por rubro")
    def rates_count(self, obj):
        # Sólo se puede cargar una vez guardado el programa (es un inline de
        # otro inline, Django no lo soporta anidado) -- por eso el link de
        # "cambiar" de la fila (show_change_link) es el camino para agregarlas.
        if not obj.pk:
            return "Guardá primero"
        n = obj.category_rates.count()
        return f"{n} cargada(s)" if n else "Ninguna (abrí el programa →)"


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
    raw_id_fields = ("bank",)
    inlines = [LoyaltyProgramInline]


@admin.register(LoyaltyProgram)
class LoyaltyProgramAdmin(BaseModelAdmin):
    """Las "Tasas por rubro" de acá abajo son la pieza que resuelve "para tal
    categoría, tal % en tal tarjeta" -- si no ves ninguna, es porque no se
    cargó ninguna todavía (arrancan vacías; sin overrides, se usa la tasa
    default de arriba para cualquier rubro)."""

    list_display = ("__str__", "card_product", "kind", "default_rate", "rates_count", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "card_product__name", "card_product__bank__name")
    raw_id_fields = ("card_product",)
    inlines = [LoyaltyCategoryRateInline]

    @admin.display(description="Tasas por rubro")
    def rates_count(self, obj):
        return obj.category_rates.count()


@admin.register(LoyaltyCategoryRate)
class LoyaltyCategoryRateAdmin(BaseModelAdmin):
    """Mismos datos que el inline de arriba (Programas de lealtad → un
    programa → Tasas por rubro) -- este listado es sólo para verlas todas
    juntas de un vistazo, sin entrar programa por programa."""

    list_display = ("program", "category_type", "rate")
    list_filter = ("category_type",)
    search_fields = ("program__name", "category_type__name")
    raw_id_fields = ("program", "category_type")


@admin.register(LoyaltyEarning)
class LoyaltyEarningAdmin(BaseModelAdmin):
    """Puntos/cashback los genera sola la señal de Transaction; descuento lo
    genera el cliente al aplicarlo -- este admin es sólo de consulta. Si
    hiciste una transacción de prueba y no aparece nada acá, revisá: la
    cartera usada tiene un producto de tarjeta asignado, la categoría tiene
    un rubro asignado, y el programa correspondiente está activo."""

    list_display = ("transaction", "program", "kind", "points", "amount", "original_amount", "created_at")
    list_filter = ("kind",)
    search_fields = ("transaction__description", "program__name")
    raw_id_fields = ("workspace", "transaction", "program")
    date_hierarchy = "created_at"
