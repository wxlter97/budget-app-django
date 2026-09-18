"""
El endpoint que el front consulta una vez para saber si la IA existe en esta
instalación y cuánto le queda al usuario este mes.

Es a propósito el único endpoint de IA por ahora: los que hacen trabajo
(recibos, parseo, chat) llegan con cada función, sobre `services.run()`. Éste
es el que permite que la app no muestre entradas de IA cuando no hay key, en
vez de mostrarlas y fallar al tocarlas.
"""
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


class QuotaSerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        allow_null=True, help_text="Tope del mes. null = sin tope."
    )
    used = serializers.IntegerField()
    remaining = serializers.IntegerField(allow_null=True)


class AIStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        help_text="False = esta instalación no tiene GEMINI_API_KEY; el cliente "
                  "debería ocultar todo lo de IA."
    )
    quotas = serializers.DictField(child=QuotaSerializer())
    resets_at = serializers.DateTimeField(
        help_text="Cuándo vuelven a cero los contadores (primer día del mes que viene)."
    )


@extend_schema(tags=["ai"], responses=AIStatusSerializer)
class AIStatusView(APIView):
    """Si la IA está disponible y cuánta cuota le queda al usuario.

    No pide `X-Workspace-ID`: la cuota es por usuario, no por presupuesto —
    quien paga es el dueño del plan, y la misma cuota se gasta desde
    cualquiera de sus workspaces.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(AIStatusSerializer(services.availability_for(request.user)).data)
