from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.api import HasWorkspaceMembership

from . import services


class BadgeStatusSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    icon = serializers.CharField()
    earned = serializers.BooleanField()


class GamificationSummarySerializer(serializers.Serializer):
    current_streak = serializers.IntegerField()
    longest_streak = serializers.IntegerField()
    no_spend_weekends = serializers.IntegerField()
    monthly_savings_pct = serializers.DecimalField(max_digits=6, decimal_places=1, allow_null=True)
    badges = BadgeStatusSerializer(many=True)


class GamificationSummaryView(APIView):
    """Racha actual/máxima de días sin gasto fuera de presupuesto, fines de
    semana sin gastos, % de ahorro del mes en curso y estado de cada badge
    -- ver `services.summary`. De paso otorga cualquier badge nuevo que ya
    se haya ganado (evaluación perezosa, sin tarea periódica)."""

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]

    def get(self, request):
        data = services.summary(request.workspace)
        return Response(GamificationSummarySerializer(data).data)
