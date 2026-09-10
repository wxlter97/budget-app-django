"""
Endpoint de solo lectura para Villa Wxlter (el dashboard/mapa que enlaza a
todos los proyectos personales). No es parte del API "normal" (JWT +
membership por workspace): lo llama un cliente sin usuario ni sesión, así
que usa su propia auth — un token fijo compartido, no un login real.

Deliberadamente NO reutiliza ``PersonalAccessToken`` (apps.quickadd): ese
modelo está pensado para un token *por integración* atado a un
usuario/wallet concreto (para poder revocar uno sin tocar los demás). Acá
solo hay un consumidor (el dashboard) y un solo dato de solo lectura, así
que un token fijo en settings alcanza y evita una tabla + migración de más.
"""
import secrets

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.workspaces.models import Workspace

from . import services

BEARER_PREFIX = "Bearer "


class DashboardNotConfigured(APIException):
    status_code = 503
    default_detail = "El endpoint del dashboard no está configurado."
    default_code = "dashboard_not_configured"


class DashboardTokenPermission(BasePermission):
    """
    Exige ``Authorization: Bearer <DASHBOARD_API_TOKEN>``.

    Si ``DASHBOARD_API_TOKEN`` no está configurado (vacío), rechaza siempre
    — nunca "sin token configurado = abierto a cualquiera".
    """

    def has_permission(self, request, view):
        expected = settings.DASHBOARD_API_TOKEN
        if not expected:
            return False

        header = request.headers.get("Authorization", "")
        if not header.startswith(BEARER_PREFIX):
            return False
        provided = header[len(BEARER_PREFIX):].strip()
        if not provided:
            return False

        # compare_digest evita timing attacks; además exige igual longitud,
        # por eso no se puede saltar con un `==` normal.
        return secrets.compare_digest(provided, expected)


def _resolve_dashboard_workspace() -> Workspace:
    """
    El workspace fijo a exponer en el dashboard.

    Con ``DASHBOARD_WORKSPACE_ID`` seteado, ese es. Si no, solo funciona
    cuando hay exactamente un Workspace (el caso de uso real, personal) —
    con 0 o 2+ sin ID explícito, es mejor fallar fuerte que adivinar cuál
    mostrar.
    """
    raw_id = settings.DASHBOARD_WORKSPACE_ID
    if raw_id:
        try:
            return Workspace.objects.get(pk=raw_id, is_deleted=False)
        except (Workspace.DoesNotExist, ValueError):
            raise DashboardNotConfigured("DASHBOARD_WORKSPACE_ID no coincide con ningún workspace.")

    workspaces = list(Workspace.objects.filter(is_deleted=False)[:2])
    if len(workspaces) != 1:
        raise DashboardNotConfigured(
            "Hay 0 o más de 1 workspace; configura DASHBOARD_WORKSPACE_ID explícito."
        )
    return workspaces[0]


class DashboardBalanceSerializer(serializers.Serializer):
    balance = serializers.DecimalField(max_digits=16, decimal_places=2)
    currency = serializers.CharField()


@extend_schema(
    tags=["dashboard"],
    responses=DashboardBalanceSerializer,
    description="Patrimonio neto actual, para el tooltip del edificio Banco en Villa Wxlter.",
)
class DashboardBalanceView(APIView):
    """`GET /api/v1/dashboard/balance/` — sin JWT, autenticado por token fijo."""

    authentication_classes = ()  # nada de JWT acá: es un cliente sin usuario
    permission_classes = [DashboardTokenPermission]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "dashboard"

    def get(self, request):
        workspace = _resolve_dashboard_workspace()
        data = services.net_worth_breakdown(workspace)
        payload = {"balance": data["net"], "currency": data["base_currency"]}
        return Response(DashboardBalanceSerializer(payload).data)
