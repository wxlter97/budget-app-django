from django.contrib import admin

from apps.common.admin import BaseModelAdmin

from .models import SupportTicket, SupportTicketMessage


class SupportTicketMessageInline(admin.TabularInline):
    model = SupportTicketMessage
    extra = 1
    fields = ("body", "author", "is_staff_reply", "created_at")
    readonly_fields = ("author", "is_staff_reply", "created_at")

    def save_formset(self, request, form, formset, change):
        # No dejamos elegir `author`/`is_staff_reply` a mano (por eso están
        # en readonly_fields): una respuesta agregada desde acá es siempre
        # de quien está logueado en el admin, y siempre de soporte.
        instances = formset.save(commit=False)
        for instance in instances:
            if instance.pk is None:
                instance.author = request.user
                instance.is_staff_reply = True
            instance.save()
        formset.save_m2m()


@admin.register(SupportTicket)
class SupportTicketAdmin(BaseModelAdmin):
    list_display = ("subject", "type", "status", "workspace", "created_by", "created_at")
    list_filter = ("type", "status")
    search_fields = ("subject", "message", "created_by__username", "created_by__email")
    raw_id_fields = ("workspace", "created_by")
    date_hierarchy = "created_at"
    inlines = [SupportTicketMessageInline]
    fields = (
        "type", "subject", "message", "status", "workspace", "created_by",
        "app_version", "platform", "id", "created_at", "updated_at",
    )
