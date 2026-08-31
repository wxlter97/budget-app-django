from django.db.models import Q
from rest_framework import serializers

from apps.common.api import WorkspaceScopedSerializerMixin, WorkspaceScopedViewSet
from apps.workspaces.models import Membership

from .models import Account, Asset, Debt, Liability


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = (
            "id",
            "name",
            "type",
            "currency",
            "opening_balance",
            "current_balance",
            "visibility",
            "owner",
            "card_last4",
            "billing_cycle_day",
            "payment_due_day",
            "is_active",
            "created_at",
            "updated_at",
        )
        # current_balance lo mantienen los signals de Transaction, no el cliente.
        read_only_fields = ("id", "current_balance", "created_at", "updated_at")

    def validate_owner(self, user):
        if user is None:
            return user
        workspace = self.context["workspace"]
        if not Membership.objects.filter(workspace=workspace, user=user).exists():
            raise serializers.ValidationError("El owner debe ser miembro del workspace.")
        return user

    def validate(self, attrs):
        visibility = attrs.get(
            "visibility",
            getattr(self.instance, "visibility", Account.VISIBILITY_SHARED),
        )
        owner = attrs.get("owner", getattr(self.instance, "owner", None))
        if visibility == Account.VISIBILITY_PRIVATE and owner is None:
            attrs["owner"] = self.context["request"].user
        if visibility == Account.VISIBILITY_SHARED:
            attrs["owner"] = None
        return attrs

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class AccountViewSet(WorkspaceScopedViewSet):
    """Cuentas del workspace activo. Las privadas solo las ve su owner."""

    serializer_class = AccountSerializer
    queryset = Account.objects.select_related("workspace", "owner").all()

    def get_queryset(self):
        user = self.request.user
        return super().get_queryset().filter(
            Q(visibility=Account.VISIBILITY_SHARED) | Q(owner=user)
        )


class AssetSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = (
            "id", "name", "type", "current_value", "visibility", "owner",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_owner(self, user):
        if user is not None:
            workspace = self.context["workspace"]
            if not Membership.objects.filter(workspace=workspace, user=user).exists():
                raise serializers.ValidationError("El owner debe ser miembro del workspace.")
        return user

    def validate(self, attrs):
        attrs = super().validate(attrs)
        visibility = attrs.get(
            "visibility", getattr(self.instance, "visibility", Account.VISIBILITY_SHARED)
        )
        if visibility == Account.VISIBILITY_PRIVATE and attrs.get(
            "owner", getattr(self.instance, "owner", None)
        ) is None:
            attrs["owner"] = self.context["request"].user
        if visibility == Account.VISIBILITY_SHARED:
            attrs["owner"] = None
        return attrs


class AssetViewSet(WorkspaceScopedViewSet):
    serializer_class = AssetSerializer
    queryset = Asset.objects.select_related("workspace", "owner").all()

    def get_queryset(self):
        user = self.request.user
        return super().get_queryset().filter(
            Q(visibility=Account.VISIBILITY_SHARED) | Q(owner=user)
        )


class LiabilitySerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Liability
        fields = (
            "id", "name", "type", "total_amount", "remaining_amount",
            "interest_rate", "due_date", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class LiabilityViewSet(WorkspaceScopedViewSet):
    serializer_class = LiabilitySerializer
    queryset = Liability.objects.select_related("workspace").all()


class DebtSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Debt
        fields = (
            "id", "direction", "person", "amount", "description", "is_settled",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class DebtViewSet(WorkspaceScopedViewSet):
    serializer_class = DebtSerializer
    queryset = Debt.objects.select_related("workspace").all()
