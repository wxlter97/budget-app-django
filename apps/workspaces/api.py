from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.api import (
    HasWorkspaceMembership,
    IsWorkspaceOwner,
    WorkspaceScopedViewSet,
)

from .models import Membership, Workspace

User = Membership._meta.get_field("user").related_model


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
class WorkspaceSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    inbound_email = serializers.ReadOnlyField()

    class Meta:
        model = Workspace
        fields = (
            "id", "name", "role", "member_count",
            "inbound_token", "inbound_email",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "inbound_token", "created_at", "updated_at")

    def get_role(self, obj) -> str:
        user = self.context["request"].user
        membership = next(
            (m for m in obj.memberships.all() if m.user_id == user.id and not m.is_deleted),
            None,
        )
        return membership.role if membership else ""

    def get_member_count(self, obj) -> int:
        return sum(1 for m in obj.memberships.all() if not m.is_deleted)


class WorkspaceOwnerOrReadOnly(IsAuthenticated):
    """Cualquier miembro lee; solo el owner modifica/borra el workspace."""

    def has_object_permission(self, request, view, obj):
        membership = next(
            (m for m in obj.memberships.all()
             if m.user_id == request.user.id and not m.is_deleted),
            None,
        )
        if membership is None:
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return membership.role == Membership.ROLE_OWNER


class WorkspaceViewSet(viewsets.ModelViewSet):
    """
    CRUD de workspaces del usuario. No usa el header X-Workspace-ID:
    la pertenencia se deduce de las Membership del usuario autenticado.
    """

    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticated, WorkspaceOwnerOrReadOnly]
    queryset = Workspace.objects.none()  # el real está en get_queryset; ayuda al esquema

    def get_queryset(self):
        return (
            Workspace.objects.filter(
                memberships__user=self.request.user,
                memberships__is_deleted=False,
            )
            .prefetch_related("memberships")
            .distinct()
        )

    @transaction.atomic
    def perform_create(self, serializer):
        workspace = serializer.save()
        Membership.objects.create(
            workspace=workspace,
            user=self.request.user,
            role=Membership.ROLE_OWNER,
        )

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=True, methods=["post"], url_path="rotate-inbound-token")
    def rotate_inbound_token(self, request, pk=None):
        """Genera un token de importación nuevo (invalida la dirección anterior). Solo owner."""
        workspace = self.get_object()
        membership = next(
            (m for m in workspace.memberships.all()
             if m.user_id == request.user.id and not m.is_deleted),
            None,
        )
        if membership is None or membership.role != Membership.ROLE_OWNER:
            raise PermissionDenied("Solo el owner puede rotar el token.")
        workspace.rotate_inbound_token()
        return Response(self.get_serializer(workspace).data)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
class MembershipSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(write_only=True, required=False)
    username = serializers.CharField(source="user.username", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = ("id", "user", "username", "user_email", "email", "role", "joined_at")
        read_only_fields = ("id", "user", "joined_at")

    def validate(self, attrs):
        workspace = self.context["workspace"]

        # --- alta: resolver el usuario por email ---
        if self.instance is None:
            email = attrs.pop("email", None)
            if not email:
                raise serializers.ValidationError({"email": "Requerido para invitar a un miembro."})
            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"email": "No hay ningún usuario registrado con ese correo."}
                )
            if Membership.objects.filter(workspace=workspace, user=user).exists():
                raise serializers.ValidationError(
                    {"email": "Ese usuario ya es miembro del workspace."}
                )
            attrs["user"] = user

        # --- no dejar al workspace sin owner ---
        if self.instance is not None and self.instance.role == Membership.ROLE_OWNER:
            new_role = attrs.get("role", self.instance.role)
            if new_role != Membership.ROLE_OWNER and self._is_last_owner(workspace):
                raise serializers.ValidationError(
                    {"role": "No puedes quitar el último owner del workspace."}
                )
        return attrs

    @staticmethod
    def _is_last_owner(workspace):
        return (
            Membership.objects.filter(
                workspace=workspace, role=Membership.ROLE_OWNER
            ).count()
            <= 1
        )

    def create(self, validated_data):
        validated_data.pop("email", None)
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class MembershipViewSet(WorkspaceScopedViewSet):
    """
    Miembros del workspace activo (header X-Workspace-ID).
    Lectura: cualquier miembro. Alta / cambio de rol / expulsión: solo el owner.
    """

    serializer_class = MembershipSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership, IsWorkspaceOwner]
    queryset = Membership.objects.select_related("user", "workspace").all()

    def perform_destroy(self, instance):
        if instance.role == Membership.ROLE_OWNER and MembershipSerializer._is_last_owner(
            instance.workspace
        ):
            from rest_framework.exceptions import ValidationError

            raise ValidationError("No puedes expulsar al último owner del workspace.")
        instance.soft_delete()
