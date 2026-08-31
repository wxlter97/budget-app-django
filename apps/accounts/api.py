from django.db.models import Q
from rest_framework import serializers

from apps.common.api import WorkspaceScopedViewSet
from apps.workspaces.models import Membership

from .models import Account


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = (
            "id",
            "name",
            "type",
            "currency",
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
        read_only_fields = ("id", "created_at", "updated_at")

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
