from django.contrib import admin

from .models import ModuleFlag


@admin.register(ModuleFlag)
class ModuleFlagAdmin(admin.ModelAdmin):
    """Togglear `is_enabled` acá mismo en la lista (sin abrir el registro) es
    el punto entero de este modelo -- ver el docstring en `models.py`."""

    list_display = ("label", "key", "is_enabled", "updated_at")
    list_editable = ("is_enabled",)
    search_fields = ("key", "label")
    readonly_fields = ("created_at", "updated_at")


class BaseModelAdmin(admin.ModelAdmin):
    """
    Admin base para los modelos que heredan de ``common.BaseModel``.

    - Muestra también los registros con soft delete (usa ``all_objects``),
      para poder inspeccionarlos y restaurarlos.
    - Deja ``id`` / ``created_at`` / ``updated_at`` como solo lectura.
    - Añade acciones de soft-delete y restaurar, y el filtro ``is_deleted``.
    """

    readonly_fields = ("id", "created_at", "updated_at")
    list_select_related = True

    def get_queryset(self, request):
        qs = self.model.all_objects.all()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def get_list_filter(self, request):
        return tuple(super().get_list_filter(request)) + ("is_deleted",)

    @admin.action(description="Soft-delete de los seleccionados")
    def soft_delete_selected(self, request, queryset):
        updated = queryset.update(is_deleted=True)
        self.message_user(request, f"{updated} registro(s) marcados como borrados.")

    @admin.action(description="Restaurar los seleccionados")
    def restore_selected(self, request, queryset):
        updated = queryset.update(is_deleted=False)
        self.message_user(request, f"{updated} registro(s) restaurados.")

    actions = ["soft_delete_selected", "restore_selected"]
