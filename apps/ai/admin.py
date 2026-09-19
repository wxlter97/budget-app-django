"""
El consumo de IA en el admin: sólo lectura.

Es un registro de lo que pasó, no una tabla que se edita — borrar o cambiar
una fila acá movería la cuota de alguien y el gasto del mes.
"""
from django.contrib import admin

from .models import AIUsage


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "user", "operation", "model", "status",
        "input_tokens", "output_tokens", "costo", "latency_ms",
    )
    list_filter = ("operation", "status", "model", "counts_against_quota")
    search_fields = ("user__username", "user__email", "error_code")
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in AIUsage._meta.fields]

    @admin.display(description="Costo estimado")
    def costo(self, obj):
        return f"${obj.cost_usd:.6f}"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
