from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import BankEmailSchema, EmailImportLog


@admin.register(BankEmailSchema)
class BankEmailSchemaAdmin(BaseModelAdmin):
    list_display = ("bank_name", "sender_pattern", "parser_version", "is_active")
    list_filter = ("is_active", "parser_version")
    search_fields = ("bank_name", "sender_pattern")


@admin.register(EmailImportLog)
class EmailImportLogAdmin(BaseModelAdmin):
    list_display = (
        "raw_email_subject",
        "workspace",
        "bank_schema",
        "status",
        "extracted_amount",
        "extracted_merchant",
        "extracted_date",
        "created_at",
    )
    list_filter = ("status", "bank_schema")
    search_fields = ("raw_email_subject", "extracted_merchant", "workspace__name")
    raw_id_fields = ("workspace", "bank_schema", "account", "resulting_transaction")
    date_hierarchy = "created_at"
