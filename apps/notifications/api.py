from django.conf import settings
from drf_spectacular.utils import extend_schema
from apps.common.api import AtomicOnlyForWritesMixin
from rest_framework import generics, mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Notification, NotificationPreference, PushDevice
from .services import send_test_push


# ---------------------------------------------------------------------------
# Dispositivos (token de Expo Push)
# ---------------------------------------------------------------------------
class PushDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushDevice
        fields = ("id", "token", "platform", "p256dh", "auth", "updated_at")
        read_only_fields = ("id", "updated_at")
        # Sin esto, DRF agrega solo un UniqueValidator sobre `token` (por su
        # `unique=True` en el modelo) que rechaza con 400 el caso normal de
        # re-registrar un token ya existente -- el upsert de abajo YA es la
        # forma correcta de manejar esa unicidad.
        extra_kwargs = {"token": {"validators": []}}

    def validate(self, attrs):
        # Web Push (RFC 8291) no puede cifrar el payload sin las dos claves
        # de la suscripción -- a diferencia de nativo, que sólo necesita el
        # token de Expo.
        if attrs.get("platform") == PushDevice.PLATFORM_WEB:
            if not attrs.get("p256dh") or not attrs.get("auth"):
                raise serializers.ValidationError(
                    "p256dh y auth son requeridos cuando platform=web."
                )
        return attrs

    def create(self, validated_data):
        # Upsert por token: reinstalar la app o cambiar de cuenta en el mismo
        # dispositivo reasigna el dueño en vez de acumular filas muertas.
        device, _ = PushDevice.objects.update_or_create(
            token=validated_data["token"],
            defaults={
                "user": self.context["request"].user,
                "platform": validated_data.get("platform", ""),
                "p256dh": validated_data.get("p256dh", ""),
                "auth": validated_data.get("auth", ""),
            },
        )
        return device


@extend_schema(tags=["notifications"])
class PushDeviceViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    """
    Registrar (o re-registrar) el token de Expo Push de este dispositivo.
    ``unregister`` es la forma normal de darlo de baja (p. ej. al cerrar
    sesión) sin que el cliente tenga que rastrear el id de la fila.
    """

    serializer_class = PushDeviceSerializer
    permission_classes = [IsAuthenticated]
    queryset = PushDevice.objects.all()

    def get_queryset(self):
        return PushDevice.objects.filter(user=self.request.user)

    @action(detail=False, methods=["get"], url_path="vapid-public-key", permission_classes=[AllowAny])
    def vapid_public_key(self, request):
        """Clave pública VAPID para `PushManager.subscribe({applicationServerKey})`
        en el navegador. Pública por diseño (viaja tal cual en la suscripción
        misma) -- no requiere estar autenticado."""
        return Response({"vapid_public_key": settings.VAPID_PUBLIC_KEY})

    @action(detail=False, methods=["post"])
    def test(self, request):
        """Aviso de prueba a todos los dispositivos del usuario. Devuelve qué pasó
        con cada uno, para distinguir "no hay dispositivos", "el servidor no pudo
        enviarlo" y "salió bien" (si aun así no se ve, el problema es del
        navegador)."""
        results = send_test_push(request.user)
        return Response({"devices": len(results), "results": results})

    @action(detail=False, methods=["post"])
    def unregister(self, request):
        token = request.data.get("token")
        if not token:
            raise ValidationError({"token": "Requerido."})
        PushDevice.objects.filter(user=request.user, token=token).delete()
        return Response(status=204)


# ---------------------------------------------------------------------------
# Preferencias (una fila por usuario, no por workspace)
# ---------------------------------------------------------------------------
class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = (
            "remind_recurring",
            "remind_installments",
            "warn_budget",
            "budget_threshold_pct",
            "remind_low_balance",
            "warn_statement_due",
            "statement_due_days_before",
            "warn_insights",
            "warn_monthly_summary",
            "warn_statement_cutoff",
            "warn_weekly_summary",
        )

    def validate_budget_threshold_pct(self, value):
        if not (50 <= value <= 100):
            raise serializers.ValidationError("Tiene que estar entre 50 y 100.")
        return value

    def validate_statement_due_days_before(self, value):
        if not (1 <= value <= 14):
            raise serializers.ValidationError("Tiene que estar entre 1 y 14 días.")
        return value


@extend_schema(tags=["notifications"])
class NotificationPreferenceView(generics.RetrieveUpdateAPIView):
    """GET/PATCH de las preferencias de avisos del usuario autenticado (se
    crean con los defaults la primera vez que se piden)."""

    serializer_class = NotificationPreferenceSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        pref, _ = NotificationPreference.objects.get_or_create(user=self.request.user)
        return pref


# ---------------------------------------------------------------------------
# Centro de notificaciones (por usuario, no por workspace)
# ---------------------------------------------------------------------------
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = (
            "id", "kind", "title", "body", "data", "status", "workspace", "created_at",
        )
        read_only_fields = fields


@extend_schema(tags=["notifications"])
class NotificationViewSet(AtomicOnlyForWritesMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Historial de notificaciones del usuario autenticado -- recordatorios
    programados que de verdad se mandaron, invitaciones a presupuestos y
    correos bancarios por revisar (ver `apps.notifications.services`). Solo
    lectura + marcar leída: las crea el propio backend, nunca el cliente.
    """

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    queryset = Notification.objects.all()

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        count = Notification.objects.filter(
            user=request.user, status=Notification.STATUS_UNREAD
        ).count()
        return Response({"count": count})

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()
        if notification.status == Notification.STATUS_UNREAD:
            notification.status = Notification.STATUS_READ
            notification.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        updated = Notification.objects.filter(
            user=request.user, status=Notification.STATUS_UNREAD
        ).update(status=Notification.STATUS_READ)
        return Response({"updated": updated})
