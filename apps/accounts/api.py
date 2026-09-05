from datetime import date as date_cls

from django.db.models import Q
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.api import WorkspaceScopedViewSet
from apps.workspaces.models import Membership

from .models import Wallet
from .services import (
    credit_card_statement,
    credit_card_statements_summary,
    goal_projection,
    recompute_wallet_balance,
)


def _parse_as_of(request):
    """(fecha, ok). `ok=False` si viene un ?as_of= inválido."""
    raw = request.query_params.get("as_of")
    if not raw:
        return timezone.localdate(), True
    try:
        return date_cls.fromisoformat(raw), True
    except ValueError:
        return None, False


class WalletSerializer(serializers.ModelSerializer):
    aggregated_balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True
    )
    progress_pct = serializers.FloatField(read_only=True, allow_null=True)
    available_credit = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True, allow_null=True
    )
    bank_name = serializers.CharField(source="bank_schema.bank_name", read_only=True, default=None)

    class Meta:
        model = Wallet
        fields = (
            "id",
            "name",
            "purpose",
            "kind",
            "color",
            "parent",
            "currency",
            "opening_balance",
            "current_balance",
            "aggregated_balance",
            "counts_toward_net_worth",
            "credit_limit",
            "available_credit",
            "goal_amount",
            "goal_date",
            "monthly_contribution",
            "progress_pct",
            "card_last4",
            "billing_cycle_day",
            "payment_due_day",
            "interest_rate",
            "due_date",
            "counterparty",
            "bank_schema",
            "bank_name",
            "visibility",
            "owner",
            "is_active",
            "is_archived",
            "sort_order",
            "is_default",
            "created_at",
            "updated_at",
        )
        # current_balance lo mantienen los signals / recompute, no el cliente.
        read_only_fields = ("id", "current_balance", "created_at", "updated_at")

    def validate_owner(self, user):
        if user is None:
            return user
        workspace = self.context["workspace"]
        if not Membership.objects.filter(workspace=workspace, user=user).exists():
            raise serializers.ValidationError("El owner debe ser miembro del workspace.")
        return user

    def validate_parent(self, parent):
        if parent is None:
            return parent
        workspace = self.context["workspace"]
        if parent.workspace_id != workspace.id:
            raise serializers.ValidationError("La cartera padre es de otro workspace.")
        # sin ciclos: subir por la cadena de padres
        node = parent
        seen = {self.instance.id} if self.instance else set()
        while node is not None:
            if node.id in seen:
                raise serializers.ValidationError("Eso crearía un ciclo de carteras.")
            seen.add(node.id)
            node = node.parent
        return parent

    def validate(self, attrs):
        visibility = attrs.get(
            "visibility",
            getattr(self.instance, "visibility", Wallet.VISIBILITY_SHARED),
        )
        owner = attrs.get("owner", getattr(self.instance, "owner", None))
        if visibility == Wallet.VISIBILITY_PRIVATE and owner is None:
            attrs["owner"] = self.context["request"].user
        if visibility == Wallet.VISIBILITY_SHARED:
            attrs["owner"] = None
        return attrs

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)

    def update(self, instance, validated_data):
        opening_changed = (
            "opening_balance" in validated_data
            and validated_data["opening_balance"] != instance.opening_balance
        )
        instance = super().update(instance, validated_data)
        if opening_changed:
            recompute_wallet_balance(instance)
        return instance


class WalletFilter(filters.FilterSet):
    class Meta:
        model = Wallet
        fields = {
            "purpose": ["exact"],
            "kind": ["exact"],
            "parent": ["exact", "isnull"],
            "is_active": ["exact"],
            "is_archived": ["exact"],
            "counts_toward_net_worth": ["exact"],
        }


class WalletReorderSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)


class GoalProjectionSerializer(serializers.Serializer):
    remaining = serializers.DecimalField(max_digits=16, decimal_places=2)
    monthly_rate = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    months_to_goal = serializers.IntegerField(allow_null=True)
    projected_date = serializers.DateField(allow_null=True)
    on_track = serializers.BooleanField(allow_null=True)


class StatementInstallmentLineSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    description = serializers.CharField()
    installments_due = serializers.IntegerField()
    installments_total = serializers.IntegerField()
    amount_due = serializers.DecimalField(max_digits=16, decimal_places=2)


class CreditCardStatementSerializer(serializers.Serializer):
    cutoff_date = serializers.DateField()
    next_cutoff_date = serializers.DateField()
    payment_due_date = serializers.DateField(allow_null=True)
    spent = serializers.DecimalField(max_digits=16, decimal_places=2)
    paid = serializers.DecimalField(max_digits=16, decimal_places=2)
    installments_due = serializers.DecimalField(max_digits=16, decimal_places=2)
    total_due = serializers.DecimalField(max_digits=16, decimal_places=2)
    current_period_spent = serializers.DecimalField(max_digits=16, decimal_places=2)
    current_period_paid = serializers.DecimalField(max_digits=16, decimal_places=2)
    installment_lines = StatementInstallmentLineSerializer(many=True)


class CreditCardStatementSummarySerializer(CreditCardStatementSerializer):
    wallet_id = serializers.UUIDField()
    wallet_name = serializers.CharField()
    currency = serializers.CharField()
    card_last4 = serializers.CharField(allow_null=True, allow_blank=True)


class WalletViewSet(WorkspaceScopedViewSet):
    """Carteras del workspace activo. Las privadas solo las ve su owner."""

    serializer_class = WalletSerializer
    filterset_class = WalletFilter
    queryset = Wallet.objects.select_related("workspace", "owner", "parent").all()

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        )
        # Las archivadas se ocultan salvo que se pidan explícitamente
        # (?is_archived=true o ?is_archived=false lo maneja el FilterSet).
        if "is_archived" not in self.request.query_params:
            qs = qs.filter(is_archived=False)
        return qs

    def _owned_wallet(self, pk):
        """Cartera del workspace (incluye archivadas), respetando privacidad."""
        user = self.request.user
        return (
            Wallet.objects.filter(workspace=self.request.workspace, id=pk)
            .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
            .first()
        )

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        wallet = self._owned_wallet(pk)
        if wallet is None:
            return Response({"detail": "No encontrada."}, status=404)
        wallet.is_archived = True
        wallet.is_default = False
        wallet.save(update_fields=["is_archived", "is_default", "updated_at"])
        return Response(self.get_serializer(wallet).data)

    @action(detail=True, methods=["post"])
    def unarchive(self, request, pk=None):
        wallet = self._owned_wallet(pk)
        if wallet is None:
            return Response({"detail": "No encontrada."}, status=404)
        wallet.is_archived = False
        wallet.save(update_fields=["is_archived", "updated_at"])
        return Response(self.get_serializer(wallet).data)

    @action(detail=True, methods=["get"])
    def projection(self, request, pk=None):
        """Proyección de la meta de ahorro de esta cartera -- ver
        `services.goal_projection`. 404 si no es una cartera de ahorro con
        meta (nada que proyectar)."""
        wallet = self._owned_wallet(pk)
        if wallet is None:
            return Response({"detail": "No encontrada."}, status=404)
        data = goal_projection(wallet)
        if data is None:
            return Response(
                {"detail": "Esta cartera no tiene una meta de ahorro."}, status=404
            )
        return Response(GoalProjectionSerializer(data).data)

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        """Estado de cuenta de esta tarjeta a una fecha dada (`?as_of=`,
        hoy por defecto) -- ver `services.credit_card_statement`."""
        wallet = self._owned_wallet(pk)
        if wallet is None:
            return Response({"detail": "No encontrada."}, status=404)
        as_of, ok = _parse_as_of(request)
        if not ok:
            return Response({"detail": "Fecha inválida."}, status=400)
        data = credit_card_statement(wallet, as_of=as_of)
        if data is None:
            return Response(
                {
                    "detail": "Esta cartera no es una tarjeta de crédito con fecha de corte configurada."
                },
                status=404,
            )
        return Response(CreditCardStatementSerializer(data).data)

    @action(detail=False, methods=["get"])
    def statements(self, request):
        """Estado de cuenta de todas las tarjetas de crédito del workspace
        (siempre a hoy) -- para el listado en Herramientas."""
        data = credit_card_statements_summary(request.workspace, request.user)
        return Response(CreditCardStatementSummarySerializer(data, many=True).data)

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        ser = WalletReorderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ids = [str(i) for i in ser.validated_data["ids"]]
        owned = {str(w.id): w for w in self.get_queryset().filter(id__in=ids)}
        for position, wid in enumerate(ids):
            wallet = owned.get(wid)
            if wallet and wallet.sort_order != position:
                wallet.sort_order = position
                wallet.save(update_fields=["sort_order", "updated_at"])
        return Response({"reordered": len(owned)})
