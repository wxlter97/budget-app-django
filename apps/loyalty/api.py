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

from apps.accounts.models import Wallet

from . import services
from .models import (
    Bank,
    CardProduct,
    CategoryType,
    LoyaltyCategoryRate,
    LoyaltyEarning,
    LoyaltyMovement,
    LoyaltyProgram,
    Merchant,
)


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


class MerchantSerializer(serializers.ModelSerializer):
    """`aliases` viaja como lista: es lo que el front necesita para reconocer
    el comercio en la descripción mientras se escribe (misma regla que
    `services.match_merchant`)."""

    aliases = serializers.SerializerMethodField()

    class Meta:
        model = Merchant
        fields = ("id", "name", "category_type", "aliases", "created_at", "updated_at")
        read_only_fields = ("id", "aliases", "created_at", "updated_at")

    def get_aliases(self, obj) -> list[str]:
        return obj.alias_list


class MerchantViewSet(viewsets.ModelViewSet):
    serializer_class = MerchantSerializer
    permission_classes = [IsAdminOrReadOnly]
    queryset = Merchant.objects.all()

    def perform_destroy(self, instance):
        instance.soft_delete()


class LoyaltyCategoryRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltyCategoryRate
        fields = ("id", "program", "category_type", "merchant", "rate", "weekday", "requires_autopay")
        read_only_fields = ("id",)

    def validate(self, attrs):
        # En un PATCH parcial, lo que no viene se toma de la instancia.
        current = self.instance
        category_type = attrs.get("category_type", getattr(current, "category_type", None))
        merchant = attrs.get("merchant", getattr(current, "merchant", None))
        if (category_type is None) == (merchant is None):
            raise serializers.ValidationError("Indicá un rubro o un comercio, no los dos ni ninguno.")
        return attrs


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
            "min_amount", "is_active", "category_rates", "created_at", "updated_at",
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
    transaction_description = serializers.CharField(source="transaction.description", read_only=True)
    transaction_date = serializers.DateField(source="transaction.date", read_only=True)

    class Meta:
        model = LoyaltyEarning
        fields = (
            "id", "transaction", "transaction_description", "transaction_date", "wallet",
            "program", "program_name", "kind", "points", "amount", "original_amount",
            "saved_amount", "created_at",
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


class LoyaltyProgramBalanceSerializer(serializers.Serializer):
    """Un programa de una tarjeta: ganado, ajustado, canjeado y disponible, en su
    unidad (`points` o `currency`, si es cashback)."""

    program = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.CharField()
    unit = serializers.CharField()
    is_active = serializers.BooleanField()
    earned = serializers.DecimalField(max_digits=14, decimal_places=2)
    adjusted = serializers.DecimalField(max_digits=14, decimal_places=2)
    redeemed = serializers.DecimalField(max_digits=14, decimal_places=2)
    available = serializers.DecimalField(max_digits=14, decimal_places=2)
    point_value = serializers.DecimalField(max_digits=8, decimal_places=4, allow_null=True)
    estimated_value = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    min_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)


class LoyaltyWalletBalanceSerializer(serializers.Serializer):
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()
    currency = serializers.CharField()
    bank = serializers.UUIDField()
    bank_name = serializers.CharField()
    product_name = serializers.CharField()
    programs = LoyaltyProgramBalanceSerializer(many=True)
    discount_saved = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_value = serializers.DecimalField(max_digits=14, decimal_places=2)


class LoyaltySummarySerializer(serializers.Serializer):
    wallets = LoyaltyWalletBalanceSerializer(many=True)
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
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(self.request.workspace, "loyalty")
        qs = super().get_queryset().filter(workspace=self.request.workspace)
        wallet = self.request.query_params.get("wallet")
        if wallet:
            qs = qs.filter(transaction__wallet_id=wallet)
        return qs.order_by("-transaction__date", "-created_at")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Saldo de puntos por cartera + cashback ganado / descuento ahorrado
        en el período (``?date_after=&date_before=``, ambos opcionales)."""
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(request.workspace, "loyalty")
        data = services.loyalty_summary(
            request.workspace,
            request.query_params.get("date_after"),
            request.query_params.get("date_before"),
        )
        return Response(LoyaltySummarySerializer(data).data)


# ---------------------------------------------------------------------------
# Canjes y ajustes (el libro de movimientos)
# ---------------------------------------------------------------------------
class LoyaltyMovementSerializer(serializers.ModelSerializer):
    program_name = serializers.CharField(source="program.name", read_only=True)

    class Meta:
        model = LoyaltyMovement
        fields = (
            "id", "wallet", "program", "program_name", "kind", "delta", "cash_value",
            "date", "note", "deposit_transaction", "created_at",
        )
        read_only_fields = fields


class LoyaltyMovementCreateSerializer(serializers.Serializer):
    """`quantity` es lo que se canjea (positivo), o el ajuste (con signo), en la unidad
    del programa: puntos, o dinero si es cashback."""

    wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all())
    program = serializers.PrimaryKeyRelatedField(queryset=LoyaltyProgram.objects.all())
    kind = serializers.ChoiceField(choices=LoyaltyMovement.KIND_CHOICES)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=2)
    date = serializers.DateField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    # Sólo canjes.
    cash_value = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    deposit_wallet = serializers.PrimaryKeyRelatedField(
        queryset=Wallet.objects.all(), required=False, allow_null=True
    )

    def validate(self, attrs):
        if attrs["kind"] == LoyaltyMovement.KIND_ADJUST and (attrs.get("cash_value") or attrs.get("deposit_wallet")):
            raise serializers.ValidationError("Un ajuste no tiene valor en dinero ni se deposita.")
        return attrs


class LoyaltyMovementUpdateSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    date = serializers.DateField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)


class LoyaltyMovementViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet,
):
    """Canjear y ajustar las recompensas ya ganadas de una tarjeta. El disponible es
    `ganado + movimientos` (ver `services.wallet_balances`): esto es lo único que se
    edita; lo ganado sale de los gastos."""

    serializer_class = LoyaltyMovementSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    queryset = LoyaltyMovement.objects.select_related("program").all()
    filterset_fields = {"wallet": ["exact"], "program": ["exact"], "kind": ["exact"]}
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        from apps.billing.services import require_feature_for_workspace

        require_feature_for_workspace(self.request.workspace, "loyalty")
        return super().get_queryset().filter(workspace=self.request.workspace)

    def create(self, request, *args, **kwargs):
        ser = LoyaltyMovementCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        common = dict(
            workspace=request.workspace, wallet=d["wallet"], program=d["program"],
            date=d.get("date"), note=d.get("note", ""), user=request.user,
        )
        if d["kind"] == LoyaltyMovement.KIND_REDEEM:
            movement = services.redeem(
                quantity=d["quantity"], cash_value=d.get("cash_value"),
                deposit_wallet=d.get("deposit_wallet"), **common,
            )
        else:
            movement = services.adjust(quantity=d["quantity"], **common)
        return Response(LoyaltyMovementSerializer(movement).data, status=201)

    def partial_update(self, request, *args, **kwargs):
        movement = self.get_object()
        ser = LoyaltyMovementUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        movement = services.update_movement(movement, **ser.validated_data)
        return Response(LoyaltyMovementSerializer(movement).data)

    def perform_destroy(self, instance):
        services.undo_movement(instance)
