from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import serializers

from apps.common.api import WorkspaceScopedViewSet
from apps.workspaces.models import Membership

from .models import Wallet
from .services import recompute_wallet_balance


class WalletSerializer(serializers.ModelSerializer):
    aggregated_balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True
    )
    progress_pct = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = Wallet
        fields = (
            "id",
            "name",
            "purpose",
            "parent",
            "currency",
            "opening_balance",
            "current_balance",
            "aggregated_balance",
            "counts_toward_net_worth",
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
            "visibility",
            "owner",
            "is_active",
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
            "parent": ["exact", "isnull"],
            "is_active": ["exact"],
            "counts_toward_net_worth": ["exact"],
        }


class WalletViewSet(WorkspaceScopedViewSet):
    """Carteras del workspace activo. Las privadas solo las ve su owner."""

    serializer_class = WalletSerializer
    filterset_class = WalletFilter
    queryset = Wallet.objects.select_related("workspace", "owner", "parent").all()

    def get_queryset(self):
        user = self.request.user
        return (
            super()
            .get_queryset()
            .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        )
