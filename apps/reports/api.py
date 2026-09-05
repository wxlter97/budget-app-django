from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

_YEAR = OpenApiParameter("year", int, description="Año (default: actual)")
_MONTH = OpenApiParameter("month", int, description="Mes 1-12 (default: actual)")

from apps.common.api import HasWorkspaceMembership, WorkspaceScopedViewSet

from . import services
from .models import MonthlySnapshot

def _Money():
    return serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)


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


# ---------------------------------------------------------------------------
# Serializers de salida de los reportes (dan tipos al esquema OpenAPI y
# fuerzan el formato string de los montos, como el resto del API)
# ---------------------------------------------------------------------------
class NetWorthByPurposeSerializer(serializers.Serializer):
    spending = _Money()
    savings = _Money()
    debt = _Money()
    asset = _Money()


class NetWorthSerializer(serializers.Serializer):
    net = _Money()
    by_purpose = NetWorthByPurposeSerializer()
    base_currency = serializers.CharField()


class BudgetRowSerializer(serializers.Serializer):
    category = serializers.UUIDField()
    category_name = serializers.CharField(allow_null=True)
    budgeted = _Money()
    spent = _Money()
    remaining = _Money()
    provision = _Money()


class BudgetTotalsSerializer(serializers.Serializer):
    budgeted = _Money()
    spent = _Money()
    remaining = _Money()


class BudgetGroupSerializer(serializers.Serializer):
    group = serializers.UUIDField(allow_null=True)
    group_name = serializers.CharField()
    budgeted = _Money()
    spent = _Money()
    remaining = _Money()
    rows = BudgetRowSerializer(many=True)


class BudgetReportSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    base_currency = serializers.CharField()
    rows = BudgetRowSerializer(many=True)
    groups = BudgetGroupSerializer(many=True)
    totals = BudgetTotalsSerializer()


class ScheduledItemSerializer(serializers.Serializer):
    date = serializers.DateField()
    kind = serializers.ChoiceField(choices=["recurring", "installment"])
    source_id = serializers.UUIDField()
    description = serializers.CharField()
    amount = _Money()
    category = serializers.UUIDField(allow_null=True)
    category_name = serializers.CharField(allow_null=True)
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()


class CashflowPointSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    income = _Money()
    expenses = _Money()
    net = _Money()


class SpendRowSerializer(serializers.Serializer):
    category = serializers.UUIDField()
    category_name = serializers.CharField(allow_null=True)
    spent = _Money()


class DashboardSummarySerializer(serializers.Serializer):
    month = CashflowPointSerializer()
    net_worth = _Money()
    base_currency = serializers.CharField()
    pending_email_imports = serializers.IntegerField()
    top_expense_categories = SpendRowSerializer(many=True)


class TrendMonthSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()


class CategoryTrendSerializer(serializers.Serializer):
    category = serializers.UUIDField()
    category_name = serializers.CharField(allow_null=True)
    # Un monto por mes, en el mismo orden que `months` de la respuesta.
    amounts = serializers.ListField(child=_Money())
    # Mes en curso vs. el anterior -- puede ser negativo (bajó).
    change = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    change_pct = serializers.FloatField(allow_null=True, read_only=True)


class CategoryTrendsSerializer(serializers.Serializer):
    months = TrendMonthSerializer(many=True)
    categories = CategoryTrendSerializer(many=True)


# ---------------------------------------------------------------------------
# Endpoints de agregación (solo lectura, workspace del header)
# ---------------------------------------------------------------------------
class _BaseReportView(APIView):
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]

    def year_month(self, request):
        today = timezone.localdate()
        try:
            year = int(request.query_params.get("year", today.year))
            month = int(request.query_params.get("month", today.month))
        except (TypeError, ValueError):
            raise ValidationError("`year` y `month` deben ser enteros.")
        if not 1 <= month <= 12:
            raise ValidationError({"month": "Debe estar entre 1 y 12."})
        return year, month


class BudgetReportView(_BaseReportView):
    """Presupuesto vs. gasto real por categoría. `?year=&month=` (default: mes actual)."""

    @extend_schema(parameters=[_YEAR, _MONTH], responses=BudgetReportSerializer)
    def get(self, request):
        year, month = self.year_month(request)
        data = services.budget_vs_actual(request.workspace, request.user, year, month)
        return Response(BudgetReportSerializer(data).data)


class NetWorthView(_BaseReportView):
    """Desglose del patrimonio neto actual."""

    @extend_schema(responses=NetWorthSerializer)
    def get(self, request):
        data = services.net_worth_breakdown(request.workspace, request.user)
        return Response(NetWorthSerializer(data).data)


class CashflowView(_BaseReportView):
    """Serie mensual de ingresos/gastos/neto. `?months=` (default 6, máx 24)."""

    @extend_schema(
        parameters=[OpenApiParameter("months", int, description="1-24 (default 6)")],
        responses=CashflowPointSerializer(many=True),
    )
    def get(self, request):
        try:
            months = int(request.query_params.get("months", 6))
        except (TypeError, ValueError):
            raise ValidationError({"months": "Debe ser un entero."})
        months = max(1, min(months, 24))
        data = services.monthly_cashflow(request.workspace, request.user, months=months)
        return Response(CashflowPointSerializer(data, many=True).data)


class CategoryTrendsView(_BaseReportView):
    """Gasto por categoría mes a mes + cuáles crecieron más. `?months=` (default 6, máx 24)."""

    @extend_schema(
        parameters=[OpenApiParameter("months", int, description="1-24 (default 6)")],
        responses=CategoryTrendsSerializer,
    )
    def get(self, request):
        try:
            months = int(request.query_params.get("months", 6))
        except (TypeError, ValueError):
            raise ValidationError({"months": "Debe ser un entero."})
        months = max(1, min(months, 24))
        data = services.category_trends(request.workspace, request.user, months=months)
        return Response(CategoryTrendsSerializer(data).data)


class DashboardSummaryView(_BaseReportView):
    """Resumen para la pantalla principal: mes actual, patrimonio, pendientes, top gastos."""

    @extend_schema(responses=DashboardSummarySerializer)
    def get(self, request):
        data = services.dashboard_summary(request.workspace, request.user)
        return Response(DashboardSummarySerializer(data).data)


class ScheduledView(_BaseReportView):
    """Transacciones programadas (recurrentes + cuotas) próximas, sin crearlas.

    `?until=YYYY-MM-DD` (default: fin del mes actual), `?since=YYYY-MM-DD`
    (default: hoy).
    """

    @extend_schema(
        parameters=[
            OpenApiParameter("until", str, description="Fecha límite (default: fin de mes)"),
            OpenApiParameter("since", str, description="Desde (default: hoy)"),
        ],
        responses=ScheduledItemSerializer(many=True),
    )
    def get(self, request):
        from datetime import date

        def _parse(name):
            raw = request.query_params.get(name)
            if not raw:
                return None
            try:
                return date.fromisoformat(raw)
            except ValueError:
                raise ValidationError({name: "Fecha ISO inválida (YYYY-MM-DD)."})

        data = services.upcoming_scheduled(
            request.workspace, request.user, until=_parse("until"), since=_parse("since")
        )
        return Response(ScheduledItemSerializer(data, many=True).data)
