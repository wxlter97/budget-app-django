from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Plan, PlanPrice, Subscription
from .providers import get_provider
from .services import active_subscription_for, apply_webhook_event, plan_for_user, redeem_promo_code


# ---------------------------------------------------------------------------
# Plan / PlanPrice -- catálogo que alimenta la pantalla de "Pasate a Pro"
# sin hardcodear nombre, límites ni precio en el cliente.
# ---------------------------------------------------------------------------
class PlanPriceSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()

    class Meta:
        model = PlanPrice
        fields = ("id", "billing_period", "amount", "currency", "is_active")

    def get_amount(self, obj) -> float:
        return obj.amount


class PlanSerializer(serializers.ModelSerializer):
    prices = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "id", "code", "name", "description", "is_default",
            "max_workspaces_owned", "max_members_per_workspace", "max_active_recurring",
            "features", "prices",
        )

    def get_prices(self, obj):
        return PlanPriceSerializer(obj.prices.filter(is_active=True), many=True).data


@extend_schema(tags=["billing"])
class PlanViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Catálogo de planes + precios activos."""

    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]
    queryset = Plan.objects.prefetch_related("prices").all()


# ---------------------------------------------------------------------------
# Mi plan / suscripción actual
# ---------------------------------------------------------------------------
class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    billing_period = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id", "plan", "billing_period", "status", "provider",
            "current_period_end", "canceled_at", "created_at",
        )

    def get_billing_period(self, obj) -> str | None:
        return obj.plan_price.billing_period if obj.plan_price else None


@extend_schema(tags=["billing"])
class MyPlanView(APIView):
    """GET: el plan efectivo del usuario autenticado + su suscripción
    vigente (si tiene una). Sin suscripción activa, ``subscription`` es
    null y ``plan`` es el plan default (gratis)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        sub = active_subscription_for(request.user)
        plan = sub.plan if sub else plan_for_user(request.user)
        return Response({
            "plan": PlanSerializer(plan).data if plan else None,
            "subscription": SubscriptionSerializer(sub).data if sub else None,
        })


# ---------------------------------------------------------------------------
# Checkout / cancelación
# ---------------------------------------------------------------------------
class CheckoutInputSerializer(serializers.Serializer):
    plan_price = serializers.PrimaryKeyRelatedField(queryset=PlanPrice.objects.filter(is_active=True))
    provider = serializers.CharField(required=False, allow_blank=True, default="")
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()


@extend_schema(tags=["billing"])
class CheckoutView(APIView):
    """POST: arranca el checkout de un precio con un proveedor de pago
    (``settings.DEFAULT_PAYMENT_PROVIDER`` si no se especifica uno).
    Devuelve la URL a la que redirigir al usuario para completar el pago."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        input_serializer = CheckoutInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        plan_price = data["plan_price"]
        provider_code = data["provider"] or settings.DEFAULT_PAYMENT_PROVIDER

        if provider_code != "manual" and not plan_price.external_ref(provider_code):
            raise ValidationError(
                {"plan_price": f"Este precio todavía no está habilitado para '{provider_code}'."}
            )

        try:
            provider = get_provider(provider_code)
        except ValueError as exc:
            raise ValidationError({"provider": str(exc)})

        subscription = Subscription.objects.create(
            user=request.user, plan=plan_price.plan, plan_price=plan_price,
            provider=provider_code, status=Subscription.STATUS_PENDING,
        )
        try:
            session = provider.create_checkout(
                user=request.user, plan_price=plan_price, subscription=subscription,
                success_url=data["success_url"], cancel_url=data["cancel_url"],
            )
        except NotImplementedError as exc:
            subscription.delete()
            raise ValidationError({"provider": str(exc)})

        if session.external_customer_id:
            subscription.external_customer_id = session.external_customer_id
            subscription.save(update_fields=["external_customer_id", "updated_at"])

        return Response({"checkout_url": session.checkout_url, "subscription_id": subscription.id})


class RedeemPromoCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=40)


@extend_schema(tags=["billing"], request=RedeemPromoCodeSerializer, responses={201: SubscriptionSerializer})
class RedeemPromoCodeView(APIView):
    """POST: canjea un código de invitación -- acceso gratis a un plan sin
    proveedor de pago (ver `services.redeem_promo_code`). Un solo canje por
    usuario en toda su vida."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        input_serializer = RedeemPromoCodeSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        subscription = redeem_promo_code(request.user, input_serializer.validated_data["code"])
        return Response(SubscriptionSerializer(subscription).data, status=201)


@extend_schema(tags=["billing"])
class CancelSubscriptionView(APIView):
    """POST: cancela la suscripción vigente del usuario autenticado."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        sub = active_subscription_for(request.user)
        if sub is None:
            raise NotFound("No tenés una suscripción activa.")

        try:
            provider = get_provider(sub.provider)
        except ValueError as exc:
            raise ValidationError({"provider": str(exc)})

        try:
            provider.cancel_subscription(sub)
        except NotImplementedError as exc:
            raise ValidationError({"provider": str(exc)})

        sub.refresh_from_db()
        return Response(SubscriptionSerializer(sub).data)


# ---------------------------------------------------------------------------
# Webhook de Wompi
# ---------------------------------------------------------------------------
@extend_schema(tags=["billing"])
class WompiWebhookView(APIView):
    """
    Recibe las notificaciones de Wompi (pago aprobado / suscripción
    renovada / cancelada / fallida) y actualiza la ``Subscription``
    correspondiente vía `services.apply_webhook_event`.

    NOTA: `WompiProvider.verify_webhook`/`parse_webhook_event` son un
    esqueleto sin confirmar contra la documentación real de Wompi todavía
    -- este endpoint falla con 501 hasta que se complete esa parte (ver
    ``apps/billing/providers.py``). No apuntar el webhook de Wompi acá en
    producción antes de eso.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "billing_webhook"

    def post(self, request):
        provider = get_provider("wompi")

        try:
            verified = provider.verify_webhook(request)
        except NotImplementedError:
            return Response(
                {"detail": "Integración de Wompi todavía no configurada."}, status=501
            )
        if not verified:
            raise PermissionDenied("Firma de webhook inválida.")

        event = provider.parse_webhook_event(request)
        apply_webhook_event(event, provider_code="wompi")
        return Response({"received": True}, status=202)
