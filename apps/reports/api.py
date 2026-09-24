import datetime as dt
from decimal import Decimal

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common import periods
from apps.common.api import AtomicOnlyForWritesMixin, HasWorkspaceMembership, WorkspaceScopedViewSet

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
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(self.request.workspace, "net_worth_history")
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


_PERIOD_START = OpenApiParameter(
    "period_start", str,
    description="YYYY-MM-DD dentro del período deseado (default: hoy). "
    "Se ajusta server-side al inicio real según `workspace.budget_period`.",
)


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
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    base_currency = serializers.CharField()
    rows = BudgetRowSerializer(many=True)
    groups = BudgetGroupSerializer(many=True)
    totals = BudgetTotalsSerializer()


class ScheduledItemSerializer(serializers.Serializer):
    date = serializers.DateField()
    kind = serializers.ChoiceField(choices=["recurring", "installment", "card_payment", "debt_due"])
    # income/expense/transfer -- de un recurrente puede salir cualquiera de
    # los 3 (ver `RecurringExpense.type`); las otras 3 `kind` siempre son
    # salidas de dinero ("expense").
    type = serializers.ChoiceField(choices=["income", "expense", "transfer"])
    source_id = serializers.UUIDField()
    description = serializers.CharField()
    amount = _Money()
    category = serializers.UUIDField(allow_null=True)
    category_name = serializers.CharField(allow_null=True)
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()
    # Solo un recurrente de tipo transferencia (p. ej. aporte automático a
    # una cartera de ahorro con meta) los trae.
    to_wallet = serializers.UUIDField(allow_null=True)
    to_wallet_name = serializers.CharField(allow_null=True)


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
    # `allow_null`: en el gratis (sin la feature "net_worth", ver
    # DashboardSummaryView.get) el patrimonio neto no se calcula ni se manda
    # -- el cliente lo trata como "sin patrimonio en tu plan", no como 0.
    net_worth = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True, allow_null=True
    )
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
class _BaseReportView(AtomicOnlyForWritesMixin, APIView):
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]

    def budget_period_start(self, request):
        """Cualquier fecha dentro del período deseado (default: hoy),
        ajustada al inicio real del período según `workspace.budget_period`
        -- mismo criterio que `CategoryBudgetSerializer.validate_period_start`."""
        raw = request.query_params.get("period_start")
        if raw is None:
            d = timezone.localdate()
        else:
            try:
                d = dt.date.fromisoformat(raw)
            except ValueError:
                raise ValidationError({"period_start": "Debe ser una fecha YYYY-MM-DD."})
        return periods.period_start(d, request.workspace.budget_period)


class BudgetReportView(_BaseReportView):
    """Presupuesto vs. gasto real por categoría, para el período de
    `workspace.budget_period` que contiene a `?period_start=` (default: hoy)."""

    @extend_schema(parameters=[_PERIOD_START], responses=BudgetReportSerializer)
    def get(self, request):
        period_start = self.budget_period_start(request)
        data = services.budget_vs_actual(request.workspace, request.user, period_start)
        return Response(BudgetReportSerializer(data).data)


class NetWorthView(_BaseReportView):
    """Desglose del patrimonio neto actual."""

    @extend_schema(responses=NetWorthSerializer)
    def get(self, request):
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(request.workspace, "net_worth")
        data = services.net_worth_breakdown(request.workspace, request.user)
        return Response(NetWorthSerializer(data).data)


class CashflowView(_BaseReportView):
    """Serie mensual de ingresos/gastos/neto. `?months=` (default 6, máx 24)."""

    @extend_schema(
        parameters=[OpenApiParameter("months", int, description="1-24 (default 6)")],
        responses=CashflowPointSerializer(many=True),
    )
    def get(self, request):
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(request.workspace, "advanced_reports")
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
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(request.workspace, "advanced_reports")
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
        from apps.billing.services import has_feature_for_workspace

        data = services.dashboard_summary(request.workspace, request.user)
        # El resto del resumen (mes, pendientes, top gastos) es del gratis --
        # sólo el patrimonio neto se calló acá (no se elimina todo el
        # endpoint, que sería tirar el resumen entero por una sola cifra).
        if not has_feature_for_workspace(request.workspace, "net_worth"):
            data["net_worth"] = None
        return Response(DashboardSummarySerializer(data).data)


class ScheduledView(_BaseReportView):
    """Transacciones programadas (recurrentes, cuotas, pago de tarjeta,
    vencimiento de deuda) en el rango pedido, sin crear nada.

    `?until=YYYY-MM-DD` (default: fin del mes actual), `?since=YYYY-MM-DD`
    (default: hoy) -- cualquier rango, no sólo hacia adelante (lo usa
    también el calendario financiero para navegar meses).

    Sin gate de `"calendar"` a propósito: alimenta también la tarjeta
    "PROGRAMADO" y los marcadores de la lista normal, que SÍ están en el
    gratis -- gatearla acá rompería esas dos cosas para todo el mundo. La
    pestaña Calendario del gratis se gatea sólo del lado del frontend (ver
    moneyapp, dashboard.tsx).
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


# ---------------------------------------------------------------------------
# Planificación (ver `planning.py`)
# ---------------------------------------------------------------------------
class MemberSpendingRowSerializer(serializers.Serializer):
    user = serializers.IntegerField(allow_null=True)
    name = serializers.CharField()
    spent = _Money()
    count = serializers.IntegerField()
    share_pct = serializers.FloatField()


class MemberSpendingSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    base_currency = serializers.CharField()
    total = _Money()
    members = MemberSpendingRowSerializer(many=True)


class MemberSpendingView(_BaseReportView):
    """Gasto del mes por miembro: quién pagó (si la transacción se dividió
    entre personas) o quién la cargó. `?year=&month=` (default: el actual)."""

    @extend_schema(
        parameters=[
            OpenApiParameter("year", int, description="Año (default: el actual)"),
            OpenApiParameter("month", int, description="1-12 (default: el actual)"),
        ],
        responses=MemberSpendingSerializer,
    )
    def get(self, request):
        from . import planning

        today = timezone.localdate()
        try:
            year = int(request.query_params.get("year", today.year))
            month = int(request.query_params.get("month", today.month))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "year y month tienen que ser enteros."})
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValidationError({"detail": "Mes o año fuera de rango."})
        data = planning.member_spending(request.workspace, request.user, year, month)
        return Response(MemberSpendingSerializer(data).data)


class CanAffordCategorySerializer(serializers.Serializer):
    category = serializers.UUIDField()
    category_name = serializers.CharField()
    budgeted = _Money()
    spent = _Money()
    remaining_before = _Money()
    remaining_after = _Money()
    has_budget = serializers.BooleanField()


class CanAffordSerializer(serializers.Serializer):
    amount = _Money()
    base_currency = serializers.CharField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    days_left = serializers.IntegerField()
    basis = serializers.ChoiceField(choices=["budget", "cashflow"])
    committed = _Money()
    available_before = _Money()
    available_after = _Money()
    category = CanAffordCategorySerializer(allow_null=True)
    verdict = serializers.ChoiceField(choices=["ok", "tight", "over"])


class CanAffordInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    category = serializers.UUIDField(required=False, allow_null=True)


class CanAffordView(_BaseReportView):
    """"¿Me alcanza?": cómo quedarían la categoría y el presupuesto del
    período si hoy se gastara `?amount=` (moneda base), opcionalmente en
    `?category=`. No crea nada."""

    @extend_schema(
        parameters=[
            OpenApiParameter("amount", str, required=True, description="Monto, en la moneda base"),
            OpenApiParameter("category", str, description="UUID de una categoría de gasto"),
        ],
        responses=CanAffordSerializer,
    )
    def get(self, request):
        from apps.transactions.models import Category

        from . import planning

        params = CanAffordInputSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        category = None
        category_id = params.validated_data.get("category")
        if category_id:
            category = Category.objects.filter(
                id=category_id, workspace=request.workspace
            ).first()
            if category is None:
                raise ValidationError({"category": "No existe en este presupuesto."})
        data = planning.can_afford(
            request.workspace, request.user, params.validated_data["amount"], category=category
        )
        return Response(CanAffordSerializer(data).data)
