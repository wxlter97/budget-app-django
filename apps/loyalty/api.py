"""
Catálogo de lealtad (Bank, CategoryType, CardProduct, LoyaltyProgram,
LoyaltyCategoryRate): global, no por workspace. Lo lee cualquier usuario
autenticado; sólo staff lo escribe (mismo criterio que
`email_import.BankEmailSchemaViewSet`).

`LoyaltyEarning` sí es por workspace: es de sólo lectura por API (lo genera
la señal de Transaction, o el propio create/update de la transacción para el
descuento -- ver `apps.transactions.api.TransactionSerializer`).
"""
from decimal import Decimal

from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.api import HasWorkspaceMembership

from . import services
from .models import Bank, CardProduct, CategoryType, LoyaltyCategoryRate, LoyaltyEarning, LoyaltyProgram


class IsAdminOrReadOnly(IsAuthenticated):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return bool(request.user and request.user.is_staff)


# ---------------------------------------------------------------------------
# Catálogo global
# ---------------------------------------------------------------------------
class BankSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bank
        fields = ("id", "name", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class BankViewSet(viewsets.ModelViewSet):
    serializer_class = BankSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = Bank.objects.all()

    def perform_destroy(self, instance):
        instance.soft_delete()


class CategoryTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryType
        fields = ("id", "slug", "name", "icon", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class CategoryTypeViewSet(viewsets.ModelViewSet):
    serializer_class = CategoryTypeSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = CategoryType.objects.all()

    def perform_destroy(self, instance):
        instance.soft_delete()


class LoyaltyCategoryRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltyCategoryRate
        fields = ("id", "program", "category_type", "rate")
        read_only_fields = ("id",)


class LoyaltyCategoryRateViewSet(viewsets.ModelViewSet):
    serializer_class = LoyaltyCategoryRateSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = LoyaltyCategoryRate.objects.select_related("program", "category_type").all()

    def perform_destroy(self, instance):
        instance.soft_delete()


class LoyaltyProgramSerializer(serializers.ModelSerializer):
    category_rates = LoyaltyCategoryRateSerializer(many=True, read_only=True)

    class Meta:
        model = LoyaltyProgram
        fields = (
            "id", "card_product", "kind", "name", "default_rate", "point_value",
            "is_active", "category_rates", "created_at", "updated_at",
        )
        read_only_fields = ("id", "category_rates", "created_at", "updated_at")


class LoyaltyProgramViewSet(viewsets.ModelViewSet):
    serializer_class = LoyaltyProgramSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = LoyaltyProgram.objects.select_related("card_product").prefetch_related("category_rates").all()
    filterset_fields = {"card_product": ["exact"], "kind": ["exact"], "is_active": ["exact"]}

    def perform_destroy(self, instance):
        instance.soft_delete()


class CardProductSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank.name", read_only=True)
    programs = LoyaltyProgramSerializer(many=True, read_only=True)

    class Meta:
        model = CardProduct
        fields = ("id", "bank", "bank_name", "name", "network", "programs", "created_at", "updated_at")
        read_only_fields = ("id", "bank_name", "programs", "created_at", "updated_at")


class CardProductViewSet(viewsets.ModelViewSet):
    serializer_class = CardProductSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = CardProduct.objects.select_related("bank").prefetch_related("programs__category_rates").all()
    filterset_fields = {"bank": ["exact"]}

    def perform_destroy(self, instance):
        instance.soft_delete()


# ---------------------------------------------------------------------------
# LoyaltyEarning + resumen  (por workspace, sólo lectura)
# ---------------------------------------------------------------------------
class LoyaltyEarningSerializer(serializers.ModelSerializer):
    wallet = serializers.UUIDField(source="transaction.wallet_id", read_only=True)
    program_name = serializers.CharField(source="program.name", read_only=True)
    saved_amount = serializers.SerializerMethodField()

    class Meta:
        model = LoyaltyEarning
        fields = (
            "id", "transaction", "wallet", "program", "program_name", "kind",
            "points", "amount", "original_amount", "saved_amount", "created_at",
        )
        read_only_fields = fields

    def get_saved_amount(self, obj) -> Decimal | None:
        return obj.discount_saved_amount


class LoyaltyPointsBalanceSerializer(serializers.Serializer):
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()
    program = serializers.UUIDField()
    program_name = serializers.CharField()
    points = serializers.DecimalField(max_digits=14, decimal_places=2)
    estimated_value = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)


class LoyaltyPeriodTotalSerializer(serializers.Serializer):
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()
    cashback_earned = serializers.DecimalField(max_digits=14, decimal_places=2)
    discount_saved = serializers.DecimalField(max_digits=14, decimal_places=2)


class LoyaltySummarySerializer(serializers.Serializer):
    points_balances = LoyaltyPointsBalanceSerializer(many=True)
    period_totals = LoyaltyPeriodTotalSerializer(many=True)


class LoyaltyEarningViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Lo que fue ganando/ahorrando cada transacción del workspace activo.
    No se crea/edita por acá: puntos y cashback los genera la señal de
    Transaction, descuento lo registra `TransactionSerializer` al crearla."""

    serializer_class = LoyaltyEarningSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    queryset = LoyaltyEarning.objects.select_related("transaction", "transaction__wallet", "program").all()
    filterset_fields = {"kind": ["exact"], "program": ["exact"]}

    def get_queryset(self):
        return super().get_queryset().filter(workspace=self.request.workspace)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Saldo de puntos por cartera + cashback ganado / descuento ahorrado
        en el período (``?date_after=&date_before=``, ambos opcionales)."""
        data = services.loyalty_summary(
            request.workspace,
            request.query_params.get("date_after"),
            request.query_params.get("date_before"),
        )
        return Response(LoyaltySummarySerializer(data).data)
