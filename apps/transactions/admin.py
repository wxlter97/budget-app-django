from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)


@admin.register(Category)
class CategoryAdmin(BaseModelAdmin):
    list_display = ("name", "workspace", "type", "parent")
    list_filter = ("type",)
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace", "parent")


@admin.register(Transaction)
class TransactionAdmin(BaseModelAdmin):
    list_display = ("description", "amount", "currency", "date", "account", "category", "source")
    list_filter = ("source", "is_recurring", "currency", "date")
    search_fields = ("description", "account__name", "category__name")
    raw_id_fields = ("account", "category", "created_by")
    date_hierarchy = "date"


@admin.register(CategoryBudget)
class CategoryBudgetAdmin(BaseModelAdmin):
    list_display = ("category", "workspace", "year", "month", "amount")
    list_filter = ("year", "month")
    search_fields = ("category__name", "workspace__name")
    raw_id_fields = ("workspace", "category")


@admin.register(CategoryProvision)
class CategoryProvisionAdmin(BaseModelAdmin):
    list_display = ("category", "accumulated_amount", "last_updated")
    search_fields = ("category__name",)
    raw_id_fields = ("category",)


@admin.register(RecurringExpense)
class RecurringExpenseAdmin(BaseModelAdmin):
    list_display = ("category", "workspace", "amount", "frequency", "next_due_date", "is_active")
    list_filter = ("frequency", "is_active")
    search_fields = ("category__name", "workspace__name")
    raw_id_fields = ("workspace", "category", "account")
    date_hierarchy = "next_due_date"


@admin.register(InstallmentPurchase)
class InstallmentPurchaseAdmin(BaseModelAdmin):
    list_display = (
        "description",
        "workspace",
        "total_amount",
        "installment_amount",
        "installments_paid",
        "installments_total",
        "start_date",
    )
    search_fields = ("description", "workspace__name")
    raw_id_fields = ("workspace", "account", "category")
    date_hierarchy = "start_date"
