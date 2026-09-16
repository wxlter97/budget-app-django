from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.api import WorkspaceScopedViewSet

from .models import SupportTicket, SupportTicketMessage
from .services import notify_new_ticket


class SupportTicketMessageSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicketMessage
        fields = ("id", "body", "is_staff_reply", "author_name", "created_at")
        read_only_fields = fields

    def get_author_name(self, obj) -> str | None:
        if obj.author is None:
            return None
        return obj.author.get_full_name() or obj.author.email


class SupportTicketSerializer(serializers.ModelSerializer):
    """`message` es el reporte original, inmutable -- las respuestas (del
    usuario o de soporte) van en `messages`, ver la acción `reply`."""

    messages = SupportTicketMessageSerializer(many=True, read_only=True)

    class Meta:
        model = SupportTicket
        fields = (
            "id",
            "type",
            "subject",
            "message",
            "status",
            "app_version",
            "platform",
            "messages",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "messages", "created_at", "updated_at")

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class SupportTicketReplySerializer(serializers.Serializer):
    message = serializers.CharField()

    def validate_message(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Requerido.")
        return value


class SupportTicketViewSet(WorkspaceScopedViewSet):
    """Tickets del usuario autenticado en el workspace activo -- cada quien
    ve sólo los suyos (no es un buzón compartido del workspace). El
    seguimiento real (cambiar `status`, responder como soporte) se hace
    desde el admin de Django, no hay panel de soporte separado."""

    serializer_class = SupportTicketSerializer
    queryset = SupportTicket.objects.select_related("created_by").prefetch_related("messages__author").all()
    filterset_fields = {"status": ["exact"], "type": ["exact"]}

    def get_queryset(self):
        return super().get_queryset().filter(created_by=self.request.user)

    def perform_create(self, serializer):
        ticket = serializer.save()
        notify_new_ticket(ticket)

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        """Agrega un mensaje del usuario al hilo del ticket (p. ej. más
        contexto después de abrirlo) -- no reenvía nada a Discord, sólo la
        apertura del ticket se notifica ahí."""
        ticket = self.get_object()
        serializer = SupportTicketReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        SupportTicketMessage.objects.create(
            ticket=ticket,
            author=request.user,
            body=serializer.validated_data["message"],
            is_staff_reply=request.user.is_staff,
        )
        # Refresca desde cero: `ticket` todavía trae en caché el prefetch de
        # `messages` de antes de crear ésta (ver `get_object()`).
        ticket = self.get_queryset().get(pk=ticket.pk)
        return Response(self.get_serializer(ticket).data)
