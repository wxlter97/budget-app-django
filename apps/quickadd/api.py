from django.core import exceptions as django_exceptions
from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.common.api import HasWorkspaceMembership
from apps.transactions.models import Category, Transaction

from .authentication import PersonalAccessTokenAuthentication
from .models import PersonalAccessToken

AUTO_CATEGORY = "auto"


# ---------------------------------------------------------------------------
# Gestión de tokens (JWT normal, desde la app — Herramientas → Atajos)
# ---------------------------------------------------------------------------
class PersonalAccessTokenSerializer(serializers.ModelSerializer):
    wallet_name = serializers.CharField(source="wallet.name", read_only=True)
    # Sólo viene poblado justo en la respuesta de `create()` (ver más abajo);
    # ni siquiera el dueño puede volver a leer el valor real después de esto.
    token = serializers.SerializerMethodField()

    class Meta:
        model = PersonalAccessToken
        fields = (
            "id", "name", "wallet", "wallet_name", "prefix", "token",
            "last_used_at", "created_at",
        )
        read_only_fields = ("id", "prefix", "token", "last_used_at", "created_at")

    def get_token(self, obj) -> str | None:
        return getattr(obj, "_raw_token", None)

    def validate_wallet(self, wallet):
        if wallet.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("La cartera es de otro workspace.")
        return wallet

    def create(self, validated_data):
        instance, raw = PersonalAccessToken.issue(
            user=self.context["request"].user,
            workspace=self.context["workspace"],
            wallet=validated_data["wallet"],
            name=validated_data["name"],
        )
        instance._raw_token = raw
        return instance


@extend_schema(tags=["quick-add"])
class PersonalAccessTokenViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Tokens personales para clientes externos (Atajos de Apple Shortcuts).
    Cada usuario sólo ve y borra los suyos propios dentro del workspace activo.
    """

    serializer_class = PersonalAccessTokenSerializer
    permission_classes = [IsAuthenticated, HasWorkspaceMembership]
    # Sin filtrar: sólo para que drf-spectacular derive el modelo sin invocar
    # get_queryset() (que necesita request.workspace, ausente al introspectar
    # el schema). El filtrado real por usuario/workspace vive abajo.
    queryset = PersonalAccessToken.objects.all()

    def get_queryset(self):
        return PersonalAccessToken.objects.filter(
            workspace=self.request.workspace, user=self.request.user
        ).select_related("wallet")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["workspace"] = self.request.workspace
        return context

    def perform_destroy(self, instance):
        # Borrado real, no soft-delete: un token revocado no tiene "papelera".
        instance.delete()


# ---------------------------------------------------------------------------
# Alta rápida (el POST que hace el Atajo)
# ---------------------------------------------------------------------------
def _resolve_category(*, workspace, txn_type, merchant, requested):
    """
    Devuelve la ``Category`` a usar, o ``None`` si no se pudo resolver
    (el llamador decide qué hacer — acá se responde con la lista completa
    para que el Atajo pueda mostrar un menú y reintentar).

    - Si ``requested`` es un id o un nombre real, se usa tal cual.
    - Si es ``"auto"`` (o no vino), se adivina por la categoría más frecuente
      entre transacciones pasadas cuya descripción contenga el comercio —
      así "aprende" de cómo se categorizó ese mismo comercio antes.
    """
    assignable = Category.objects.filter(
        workspace=workspace, type=txn_type, parent__isnull=False
    )

    if requested and requested != AUTO_CATEGORY:
        try:
            match = assignable.filter(pk=requested).first()
        except (ValueError, django_exceptions.ValidationError):
            match = None
        if match is None:
            match = assignable.filter(name__iexact=requested).first()
        return match

    if not merchant:
        return None

    best = (
        Transaction.objects.filter(
            wallet__workspace=workspace,
            type=txn_type,
            category__isnull=False,
            description__icontains=merchant,
        )
        .values("category")
        .annotate(n=Count("category"))
        .order_by("-n")
        .first()
    )
    if not best:
        return None
    return assignable.filter(pk=best["category"]).first()


class QuickAddSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    merchant = serializers.CharField(max_length=255)
    # Id de categoría, nombre exacto, o "auto" (default) para adivinar por
    # comercio. Si no se puede adivinar, la respuesta trae `categories` con
    # las opciones para que el Atajo pregunte y reintente con el id elegido.
    category = serializers.CharField(required=False, default=AUTO_CATEGORY, allow_blank=True)
    type = serializers.ChoiceField(
        choices=[Transaction.TYPE_EXPENSE, Transaction.TYPE_INCOME],
        default=Transaction.TYPE_EXPENSE,
    )
    date = serializers.DateField(required=False)


class QuickAddResultSerializer(serializers.Serializer):
    transaction_id = serializers.UUIDField()
    category = serializers.CharField()
    category_id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    wallet = serializers.CharField()


@extend_schema(
    tags=["quick-add"],
    request=QuickAddSerializer,
    responses={201: QuickAddResultSerializer},
)
class QuickAddView(APIView):
    """
    Alta rápida de una transacción desde un cliente externo (el Atajo de
    Apple Shortcuts que lee la notificación de Apple Pay). Autenticación por
    ``PersonalAccessToken`` — se genera en Herramientas → Atajos; ese mismo
    token trae fijos el workspace y la cartera, así el body sólo necesita
    monto y comercio.

    Si ``category`` se omite (o es ``"auto"``) y no hay suficiente historial
    para adivinarla, responde 400 con la lista de categorías del workspace
    para que el Atajo muestre un menú y reintente con el id elegido.
    """

    authentication_classes = [PersonalAccessTokenAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quick_add"

    def post(self, request):
        serializer = QuickAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        token: PersonalAccessToken = request.quickadd_token
        workspace = token.workspace
        wallet = token.wallet
        txn_type = data["type"]

        category = _resolve_category(
            workspace=workspace,
            txn_type=txn_type,
            merchant=data["merchant"],
            requested=data.get("category") or AUTO_CATEGORY,
        )
        if category is None:
            options = Category.objects.filter(
                workspace=workspace, type=txn_type, parent__isnull=False
            ).values("id", "name")
            raise ValidationError({
                "category": "No pude adivinar la categoría — elegí una y reintentá con su id.",
                "categories": list(options),
            })

        txn = Transaction.objects.create(
            type=txn_type,
            wallet=wallet,
            category=category,
            amount=data["amount"],
            currency=wallet.currency,
            description=data["merchant"],
            date=data.get("date") or timezone.localdate(),
            created_by=token.user,
            source=Transaction.SOURCE_QUICK_ADD,
        )
        return Response(
            {
                "transaction_id": txn.id,
                "category": category.name,
                "category_id": category.id,
                "amount": txn.amount,
                "wallet": wallet.name,
            },
            status=201,
        )
