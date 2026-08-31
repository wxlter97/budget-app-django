"""
Infraestructura compartida del API v1.

El API es multi-tenant: cada request opera sobre un único workspace,
identificado por el header ``X-Workspace-ID``. La resolución y validación
del workspace vive en :class:`HasWorkspaceMembership`; los viewsets de
dominio heredan de :class:`WorkspaceScopedViewSet`, que filtra y asigna el
workspace automáticamente.
"""
import uuid

from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated

WORKSPACE_HEADER = "X-Workspace-ID"


def resolve_workspace(request):
    """
    Devuelve ``(workspace, membership)`` para el usuario autenticado según el
    header ``X-Workspace-ID``.

    - 400 si el header falta o no es un UUID.
    - 403 si el usuario no es miembro (o el workspace no existe / está borrado).
      Se responde 403 y no 404 a propósito: un usuario ajeno no debe poder
      distinguir "no existe" de "no tienes acceso".
    """
    # Import diferido para evitar ciclos (workspaces -> common.api -> workspaces).
    from apps.workspaces.models import Membership

    raw = request.headers.get(WORKSPACE_HEADER)
    if not raw:
        raise ValidationError({WORKSPACE_HEADER: "Header requerido."})
    try:
        workspace_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise ValidationError({WORKSPACE_HEADER: "No es un UUID válido."})

    try:
        membership = Membership.objects.select_related("workspace").get(
            workspace_id=workspace_id,
            user=request.user,
            workspace__is_deleted=False,
        )
    except Membership.DoesNotExist:
        raise PermissionDenied("No perteneces a este workspace o no existe.")

    return membership.workspace, membership


class HasWorkspaceMembership(BasePermission):
    """
    Exige un ``X-Workspace-ID`` válido del que el usuario sea miembro.
    Como efecto lateral deja ``request.workspace`` y ``request.membership``.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        request.workspace, request.membership = resolve_workspace(request)
        return True

    def has_object_permission(self, request, view, obj):
        # Defensa en profundidad: aunque get_queryset ya filtra por workspace,
        # revalidamos que el objeto pertenezca al workspace del header.
        workspace_field = getattr(view, "workspace_field", "workspace")
        return _workspace_id_of(obj, workspace_field) == request.workspace.id


class IsWorkspaceOwner(BasePermission):
    """Solo escritura para el owner del workspace; lectura para cualquier miembro."""

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

    def has_permission(self, request, view):
        if request.method in self.SAFE_METHODS:
            return True
        membership = getattr(request, "membership", None)
        return bool(membership and membership.role == membership.ROLE_OWNER)


def _workspace_id_of(obj, workspace_field):
    """Sigue ``workspace_field`` (p. ej. 'account__workspace') hasta el Workspace."""
    value = obj
    for part in workspace_field.split("__"):
        value = getattr(value, part)
    return getattr(value, "id", value)


class WorkspaceScopedViewSet(viewsets.ModelViewSet):
    """
    Base para los viewsets de dominio.

    - ``workspace_field``: ruta ORM desde el modelo hasta el Workspace
      (``"workspace"`` por defecto; p. ej. ``"account__workspace"`` para
      Transaction).
    - Filtra el queryset por el workspace del header.
    - Pasa ``workspace`` al serializer vía contexto para validaciones.
    - DELETE hace soft delete.
    """

    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    workspace_field = "workspace"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(**{self.workspace_field: self.request.workspace})

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if getattr(self.request, "workspace", None) is not None:
            context["workspace"] = self.request.workspace
            context["membership"] = self.request.membership
        return context

    def perform_destroy(self, instance):
        instance.soft_delete()
