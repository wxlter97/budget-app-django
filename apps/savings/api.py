from rest_framework import serializers

from apps.common.api import WorkspaceScopedSerializerMixin, WorkspaceScopedViewSet

from .models import ReserveFund, SavingsGoal


class SavingsGoalSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    progress_pct = serializers.FloatField(read_only=True)

    class Meta:
        model = SavingsGoal
        fields = (
            "id", "name", "target_amount", "current_amount", "target_date",
            "monthly_contribution_suggested", "progress_pct",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "progress_pct", "created_at", "updated_at")


class SavingsGoalViewSet(WorkspaceScopedViewSet):
    serializer_class = SavingsGoalSerializer
    queryset = SavingsGoal.objects.select_related("workspace").all()


class ReserveFundSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ReserveFund
        fields = ("id", "name", "current_amount", "monthly_contribution", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class ReserveFundViewSet(WorkspaceScopedViewSet):
    serializer_class = ReserveFundSerializer
    queryset = ReserveFund.objects.select_related("workspace").all()
