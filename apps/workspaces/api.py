from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.api import (
    HasWorkspaceMembership,
    IsWorkspaceOwner,
    WorkspaceScopedViewSet,
)

from .models import ExchangeRate, Invitation, Membership, Workspace
from .services import get_or_create_invitation, send_invitation_email

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
            "base_currency",
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
        self._require_owner(workspace, request.user)
        workspace.rotate_inbound_token()
        return Response(self.get_serializer(workspace).data)

    @action(detail=True, methods=["post"])
    def reset(self, request, pk=None):
        """Borra los datos del workspace. Solo owner. Irreversible.

        Body: ``{"scope": "movimientos" | "todo", "confirm": true}``.
        - ``movimientos`` (default): borra transacciones, recurrentes, cuotas,
          presupuestos y snapshots; deja carteras, categorías y etiquetas, y
          resetea el saldo de cada cartera a su ``opening_balance``.
        - ``todo``: además borra carteras, categorías y etiquetas.
        """
        from django.db import transaction as db_transaction

        from .services import wipe_workspace_data

        workspace = self.get_object()
        self._require_owner(workspace, request.user)

        scope = request.data.get("scope", "movimientos")
        if scope not in ("movimientos", "todo"):
            raise serializers.ValidationError({"scope": "Debe ser 'movimientos' o 'todo'."})
        if request.data.get("confirm") is not True:
            raise serializers.ValidationError(
                {"confirm": "Enviá confirm=true para ejecutar el reinicio."}
            )

        with db_transaction.atomic():
            deleted = wipe_workspace_data(workspace, scope=scope)

        return Response({"scope": scope, "deleted": deleted})

    @action(detail=True, methods=["get"])
    def backup(self, request, pk=None):
        """Respaldo completo del workspace en JSON (carteras, categorías,
        etiquetas, presupuestos, recurrentes, compras a plazo y
        transacciones -- sin fotos de recibo) para descargar y, más
        adelante, restaurar con `restore`. Solo owner."""
        from .services import export_backup

        workspace = self.get_object()
        self._require_owner(workspace, request.user)
        return Response(export_backup(workspace))

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """Restaura este workspace desde un respaldo de `backup`.
        IRREVERSIBLE: primero borra TODO lo que hay en el workspace (como
        `reset` con ``scope=todo``) y después recrea todo desde el archivo.
        Solo owner. Body: ``{..backup.., "confirm": true}``."""
        from .services import BackupError, import_backup

        workspace = self.get_object()
        self._require_owner(workspace, request.user)

        if request.data.get("confirm") is not True:
            raise serializers.ValidationError(
                {"confirm": "Enviá confirm=true para ejecutar la restauración."}
            )

        try:
            summary = import_backup(workspace, request.data, request.user)
        except BackupError as exc:
            raise serializers.ValidationError({"detail": str(exc)})

        return Response({"restored": summary})

    def _require_owner(self, workspace, user):
        membership = next(
            (m for m in workspace.memberships.all()
             if m.user_id == user.id and not m.is_deleted),
            None,
        )
        if membership is None or membership.role != Membership.ROLE_OWNER:
            raise PermissionDenied("Solo el owner puede hacer esto.")


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = ("id", "user", "username", "user_email", "role", "joined_at")
        read_only_fields = ("id", "user", "joined_at")

    def validate(self, attrs):
        workspace = self.context["workspace"]
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


class InviteInputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Membership.ROLE_CHOICES, default=Membership.ROLE_MEMBER)


class MembershipViewSet(WorkspaceScopedViewSet):
    """
    Miembros del workspace activo (header X-Workspace-ID).
    Lectura: cualquier miembro. Alta / cambio de rol / expulsión: solo el owner.
    """

    serializer_class = MembershipSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership, IsWorkspaceOwner]
    queryset = Membership.objects.select_related("user", "workspace").all()

    def create(self, request, *args, **kwargs):
        """
        Si ya existe una cuenta con ese correo, se agrega directo (como
        antes): 201 con la Membership. Si no existe, en vez de rechazar con
        400 -- obligando a que la otra persona ya tuviera cuenta creada --
        se crea (o reutiliza) una Invitation y se le manda un correo con el
        enlace para sumarse; responde 202 con la Invitation para que el
        cliente distinga "ya quedó adentro" de "le mandamos un correo".
        """
        input_serializer = InviteInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        email = input_serializer.validated_data["email"]
        role = input_serializer.validated_data["role"]
        workspace = request.workspace

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            invitation = get_or_create_invitation(workspace, email, role, invited_by=request.user)
            send_invitation_email(invitation)
            return Response(InvitationSerializer(invitation).data, status=202)

        if Membership.objects.filter(workspace=workspace, user=user).exists():
            raise serializers.ValidationError(
                {"email": "Ese usuario ya es miembro del workspace."}
            )

        membership = Membership.objects.create(workspace=workspace, user=user, role=role)
        return Response(self.get_serializer(membership).data, status=201)

    def perform_destroy(self, instance):
        if instance.role == Membership.ROLE_OWNER and MembershipSerializer._is_last_owner(
            instance.workspace
        ):
            from rest_framework.exceptions import ValidationError

            raise ValidationError("No puedes expulsar al último owner del workspace.")
        instance.soft_delete()


# ---------------------------------------------------------------------------
# Invitation  (invitaciones a alguien sin cuenta todavía; no va por header
# X-Workspace-ID -- el invitado ni siquiera es miembro de nada aún)
# ---------------------------------------------------------------------------
class InvitationSerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    invited_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = (
            "id", "workspace", "workspace_name", "email", "role", "status",
            "token", "invited_by_name", "created_at", "responded_at",
        )
        read_only_fields = fields

    def get_invited_by_name(self, obj) -> str | None:
        if not obj.invited_by:
            return None
        return obj.invited_by.get_full_name() or obj.invited_by.username


class InvitationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Invitaciones DEL USUARIO AUTENTICADO (resueltas por su email, no por el
    header X-Workspace-ID -- todavía no es miembro de ese workspace).
    `retrieve` (por `token`) es público: así el enlace del correo se puede
    abrir sin haber iniciado sesión y mostrar a qué te invitaron.
    """

    serializer_class = InvitationSerializer
    lookup_field = "token"
    queryset = Invitation.objects.select_related("workspace", "invited_by").all()

    def get_permissions(self):
        if self.action == "retrieve":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list":
            return qs.filter(
                email__iexact=self.request.user.email, status=Invitation.STATUS_PENDING
            )
        return qs

    @action(detail=True, methods=["post"])
    def accept(self, request, token=None):
        invitation = self.get_object()
        self._require_own_pending_invitation(invitation, request.user)
        with transaction.atomic():
            Membership.objects.get_or_create(
                workspace=invitation.workspace,
                user=request.user,
                defaults={"role": invitation.role},
            )
            invitation.status = Invitation.STATUS_ACCEPTED
            invitation.responded_at = timezone.now()
            invitation.save(update_fields=["status", "responded_at", "updated_at"])
        return Response(self.get_serializer(invitation).data)

    @action(detail=True, methods=["post"])
    def decline(self, request, token=None):
        invitation = self.get_object()
        self._require_own_pending_invitation(invitation, request.user)
        invitation.status = Invitation.STATUS_DECLINED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=["status", "responded_at", "updated_at"])
        return Response(self.get_serializer(invitation).data)

    @staticmethod
    def _require_own_pending_invitation(invitation, user):
        if invitation.status != Invitation.STATUS_PENDING:
            raise serializers.ValidationError(
                f"La invitación ya está {invitation.get_status_display().lower()}."
            )
        if invitation.email.lower() != (user.email or "").lower():
            raise PermissionDenied("Esta invitación es para otro correo.")


# ---------------------------------------------------------------------------
# ExchangeRate
# ---------------------------------------------------------------------------
class ExchangeRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExchangeRate
        fields = ("id", "currency", "rate_to_base", "updated_at")
        read_only_fields = ("id", "updated_at")
        # Sin esto, el `unique_together` implícito de la UniqueConstraint del
        # modelo rechazaría con 400 el caso normal de actualizar la tasa de
        # una moneda ya cargada -- el upsert de `create()` ya cubre eso.
        extra_kwargs = {"currency": {"validators": []}}

    def validate_currency(self, value):
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError("Tiene que ser un código ISO 4217 de 3 letras (p. ej. EUR).")
        return value

    def validate_rate_to_base(self, value):
        if value <= 0:
            raise serializers.ValidationError("Tiene que ser mayor que 0.")
        return value

    def validate(self, attrs):
        workspace = self.context["workspace"]
        currency = attrs.get("currency", getattr(self.instance, "currency", None))
        if currency == workspace.base_currency:
            raise serializers.ValidationError(
                {"currency": f"Ya es la moneda base del workspace ({workspace.base_currency})."}
            )
        return attrs

    def create(self, validated_data):
        # Upsert por (workspace, currency): cargar una tasa para una moneda
        # que ya tenía una simplemente la actualiza, en vez de rechazar con
        # un error de unicidad -- es el flujo normal de "corregir la tasa".
        rate, _ = ExchangeRate.objects.update_or_create(
            workspace=self.context["workspace"],
            currency=validated_data["currency"],
            defaults={"rate_to_base": validated_data["rate_to_base"]},
        )
        return rate


class ExchangeRateViewSet(WorkspaceScopedViewSet):
    """
    Tasas de cambio manuales del workspace activo, para expresar los totales
    agregados (patrimonio neto, presupuesto, flujo de caja) en una sola
    moneda cuando hay carteras en más de una. Cualquier miembro puede
    cargarlas -- no son destructivas como para restringirlas al owner.
    """

    serializer_class = ExchangeRateSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    queryset = ExchangeRate.objects.all()

    def perform_destroy(self, instance):
        # Hard delete: si fuera soft-delete, la UniqueConstraint (workspace,
        # currency) seguiría "ocupada" por la fila borrada y no se podría
        # volver a cargar una tasa para esa misma moneda.
        instance.delete()
