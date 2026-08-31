from rest_framework import mixins, serializers, viewsets

from apps.common.api import WorkspaceScopedViewSet

from .models import MonthlySnapshot


class MonthlySnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MonthlySnapshot
        fields = (
            "id", "month", "year", "total_net_worth", "total_income",
            "total_expenses", "created_at",
        )
        read_only_fields = fields


class MonthlySnapshotViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """
    Histórico mensual del workspace activo. Solo lectura: los snapshots los
    genera la tarea de cierre de mes (Celery Beat), no el cliente.
    """

    serializer_class = MonthlySnapshotSerializer
    permission_classes = WorkspaceScopedViewSet.permission_classes
    queryset = MonthlySnapshot.objects.select_related("workspace").all()

    def get_queryset(self):
        return super().get_queryset().filter(workspace=self.request.workspace)
