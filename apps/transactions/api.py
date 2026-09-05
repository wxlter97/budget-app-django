import mimetypes
import uuid
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import Wallet
from apps.common.api import WorkspaceScopedSerializerMixin, WorkspaceScopedViewSet

from . import services
from .models import (
    Category,
    CategoryBudget,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)


def _visible_wallets(workspace, user):
    return Wallet.objects.filter(workspace=workspace).filter(
        Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user)
    )


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------
class CategorySerializer(serializers.ModelSerializer):
    is_group = serializers.BooleanField(read_only=True)

    class Meta:
        model = Category
        fields = (
            "id", "name", "icon", "color", "type", "parent", "sort_order",
            "is_group", "created_at", "updated_at",
        )
        read_only_fields = ("id", "is_group", "created_at", "updated_at")

    def validate_parent(self, parent):
        if parent is None:
            return parent
        if parent.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("La categoría padre es de otro workspace.")
        if parent.parent_id is not None:
            raise serializers.ValidationError(
                "El padre debe ser un grupo (una categoría de primer nivel)."
            )
        if self.instance is not None and parent.id == self.instance.id:
            raise serializers.ValidationError("Una categoría no puede ser su propio grupo.")
        return parent

    def validate(self, attrs):
        # Si la categoría tiene subcategorías, no puede volverse hija de otra
        # (solo se permiten 2 niveles).
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if (
            parent is not None
            and self.instance is not None
            and self.instance.subcategories.exists()
        ):
            raise serializers.ValidationError(
                {"parent": "Este grupo tiene subcategorías; no puede volverse subcategoría."}
            )
        if parent is not None:
            attrs.setdefault("type", parent.type if not self.instance else self.instance.type)
        return attrs

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class CategoryReorderSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)


class CategoryViewSet(WorkspaceScopedViewSet):
    serializer_class = CategorySerializer
    queryset = Category.objects.select_related("workspace", "parent").all()
    filterset_fields = {"type": ["exact"], "parent": ["exact", "isnull"]}

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """`{"ids": [...]}` — fija `sort_order` según el orden recibido."""
        ser = CategoryReorderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ids = [str(i) for i in ser.validated_data["ids"]]
        owned = {
            str(c.id): c
            for c in Category.objects.filter(
                workspace=request.workspace, id__in=ids
            )
        }
        for position, cid in enumerate(ids):
            cat = owned.get(cid)
            if cat and cat.sort_order != position:
                cat.sort_order = position
                cat.save(update_fields=["sort_order", "updated_at"])
        return Response({"reordered": len(owned)})

    @action(detail=False, methods=["get"])
    def deleted(self, request):
        """Categorías soft-deleted del workspace (para restaurarlas)."""
        qs = Category.all_objects.filter(
            workspace=request.workspace, is_deleted=True
        ).select_related("parent")
        return Response(CategorySerializer(qs, many=True, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        cat = Category.all_objects.filter(
            workspace=request.workspace, id=pk, is_deleted=True
        ).first()
        if cat is None:
            return Response({"detail": "No encontrada."}, status=404)
        cat.is_deleted = False
        cat.save(update_fields=["is_deleted", "updated_at"])
        return Response(CategorySerializer(cat, context=self.get_serializer_context()).data)


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------
class TransactionSerializer(serializers.ModelSerializer):
    # `type` es opcional al escribir: si se omite en income/expense se deduce
    # de la categoría. Requerido para transferencias.
    type = serializers.ChoiceField(
        choices=Transaction.TYPE_CHOICES, required=False
    )
    # El archivo en sí no viaja acá (se sube/lee por la acción `receipt`,
    # ver más abajo) — esto es sólo para que el cliente sepa si mostrar el
    # ícono de "tiene recibo" sin pedir el binario.
    has_receipt = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = (
            "id",
            "type",
            "wallet",
            "to_wallet",
            "category",
            "amount",
            "currency",
            "description",
            "date",
            "has_receipt",
            "counts_toward_budget",
            "source",
            "is_recurring",
            "split_group",
            "created_by",
            "created_at",
            "updated_at",
        )
        # split_group no se manda nunca a mano: sólo lo asigna la acción
        # `split` (ver más abajo) al partir una transacción en varias.
        read_only_fields = (
            "id", "currency", "split_group", "created_by", "created_at", "updated_at",
        )

    def get_has_receipt(self, obj) -> bool:
        return bool(obj.receipt)

    def _check_wallet(self, wallet, workspace, user, field):
        if wallet is None:
            return
        if wallet.workspace_id != workspace.id:
            raise serializers.ValidationError({field: "La cartera es de otro workspace."})
        if (
            wallet.visibility == Wallet.VISIBILITY_PRIVATE
            and wallet.owner_id != user.id
        ):
            raise serializers.ValidationError(
                {field: "No puedes usar una cartera privada ajena."}
            )

    def validate(self, attrs):
        workspace = self.context["workspace"]
        user = self.context["request"].user
        inst = self.instance

        wallet = attrs.get("wallet") or getattr(inst, "wallet", None)
        to_wallet = attrs.get("to_wallet", getattr(inst, "to_wallet", None))
        category = attrs.get("category", getattr(inst, "category", None))
        txn_type = attrs.get("type") or getattr(inst, "type", None)

        # Deduce el tipo de la categoría si no viene y no es transferencia.
        if not txn_type and category is not None:
            txn_type = category.type
            attrs["type"] = txn_type
        if not txn_type:
            raise serializers.ValidationError(
                {"type": "Requerido (o envía una categoría de la que deducirlo)."}
            )

        self._check_wallet(wallet, workspace, user, "wallet")

        if txn_type == Transaction.TYPE_TRANSFER:
            if to_wallet is None:
                raise serializers.ValidationError(
                    {"to_wallet": "Requerida en una transferencia."}
                )
            if wallet is not None and to_wallet.id == wallet.id:
                raise serializers.ValidationError(
                    {"to_wallet": "La cartera destino debe ser distinta de la origen."}
                )
            self._check_wallet(to_wallet, workspace, user, "to_wallet")
            attrs["to_wallet"] = to_wallet
            # La categoría es opcional en transferencias (p. ej. mover a
            # "Ahorro"). Si viene, se valida su workspace; su `type` no importa.
            if category is not None:
                if category.workspace_id != workspace.id:
                    raise serializers.ValidationError(
                        {"category": "La categoría es de otro workspace."}
                    )
            else:
                attrs["counts_toward_budget"] = False
        else:
            if category is None:
                raise serializers.ValidationError(
                    {"category": "Requerida en ingresos y gastos."}
                )
            if category.workspace_id != workspace.id:
                raise serializers.ValidationError(
                    {"category": "La categoría es de otro workspace."}
                )
            if category.type != txn_type:
                raise serializers.ValidationError(
                    {"category": f"La categoría no es de tipo «{txn_type}»."}
                )
            attrs["to_wallet"] = None

        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


class TransactionSplitPartSerializer(serializers.Serializer):
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all())
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Tiene que ser mayor que 0.")
        return value


class TransactionSplitSerializer(serializers.Serializer):
    """Body de la acción `split`: al menos 2 partes, cada una con su propia
    categoría y monto. La suma exacta contra el monto original se valida
    en la vista (ahí ya se conoce la transacción a dividir)."""

    parts = TransactionSplitPartSerializer(many=True)

    def validate_parts(self, parts):
        if len(parts) < 2:
            raise serializers.ValidationError("Hacen falta al menos 2 partes.")
        return parts


class TransactionFilter(filters.FilterSet):
    """Filtros de querystring para la lista de transacciones.

    Ej.: ``?date_after=2026-08-01&date_before=2026-08-31&source=manual``
    """

    date_after = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_before = filters.DateFilter(field_name="date", lookup_expr="lte")
    amount_min = filters.NumberFilter(field_name="amount", lookup_expr="gte")
    amount_max = filters.NumberFilter(field_name="amount", lookup_expr="lte")
    search = filters.CharFilter(method="filter_search")

    def filter_search(self, queryset, name, value):
        """Coincidencia parcial sobre descripción, categoría o cartera."""
        return queryset.filter(
            Q(description__icontains=value)
            | Q(category__name__icontains=value)
            | Q(wallet__name__icontains=value)
        )

    class Meta:
        model = Transaction
        fields = {
            "type": ["exact"],
            "wallet": ["exact"],
            "to_wallet": ["exact"],
            "category": ["exact"],
            "source": ["exact"],
            "is_recurring": ["exact"],
            "counts_toward_budget": ["exact"],
            "split_group": ["exact"],
        }


class TransactionViewSet(WorkspaceScopedViewSet):
    """Transacciones del workspace activo (scoping vía wallet__workspace)."""

    serializer_class = TransactionSerializer
    workspace_field = "wallet__workspace"
    filterset_class = TransactionFilter
    queryset = Transaction.objects.select_related(
        "wallet", "to_wallet", "category", "created_by"
    ).all()

    def get_queryset(self):
        user = self.request.user
        return super().get_queryset().filter(
            Q(wallet__visibility=Wallet.VISIBILITY_SHARED) | Q(wallet__owner=user)
        )

    RECEIPT_MAX_SIZE = 8 * 1024 * 1024  # 8 MB
    RECEIPT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

    # Una sola URL (`/transactions/{id}/receipt/`), tres métodos: subir
    # (reemplaza si ya había uno), ver el archivo, y borrarlo. `get_object()`
    # ya aplica el scoping de workspace del viewset — no hace falta repetirlo.
    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser])
    def receipt(self, request, pk=None):
        txn = self.get_object()
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"file": "Requerido."})
        if file.size > self.RECEIPT_MAX_SIZE:
            raise ValidationError({"file": "El archivo pesa más de 8 MB."})
        if file.content_type not in self.RECEIPT_CONTENT_TYPES:
            raise ValidationError({"file": "Formato no soportado (usá JPG, PNG, WEBP o HEIC)."})

        if txn.receipt:
            txn.receipt.delete(save=False)
        txn.receipt.save(file.name, file, save=True)
        return Response(self.get_serializer(txn).data)

    @receipt.mapping.get
    def receipt_download(self, request, pk=None):
        txn = self.get_object()
        if not txn.receipt:
            raise NotFound("Esta transacción no tiene recibo.")
        content_type = mimetypes.guess_type(txn.receipt.name)[0] or "application/octet-stream"
        return FileResponse(txn.receipt.open("rb"), content_type=content_type)

    @receipt.mapping.delete
    def receipt_remove(self, request, pk=None):
        txn = self.get_object()
        if txn.receipt:
            txn.receipt.delete(save=False)
            txn.receipt = None
            txn.save(update_fields=["receipt", "updated_at"])
        return Response(status=204)

    @action(detail=True, methods=["post"])
    def split(self, request, pk=None):
        """
        Divide esta transacción (gasto o ingreso, no transferencia) en
        varias partes, cada una con su propia categoría y monto -- p. ej.
        una compra de supermercado repartida entre "Comida" e "Higiene".
        Las partes tienen que sumar exactamente el monto original.

        Reemplaza la transacción original (soft-delete) por N transacciones
        nuevas, mismas cartera/fecha, unidas por `split_group` -- el saldo
        de la cartera y los reportes por categoría no necesitan ningún caso
        especial: cada parte es una Transaction real e independiente.
        """
        txn = self.get_object()
        if txn.type == Transaction.TYPE_TRANSFER:
            raise ValidationError("No se puede dividir una transferencia.")
        if txn.split_group:
            raise ValidationError("Esta transacción ya está dividida.")

        serializer = TransactionSplitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parts = serializer.validated_data["parts"]

        workspace = self.request.workspace
        for p in parts:
            cat = p["category"]
            if cat.workspace_id != workspace.id:
                raise ValidationError({"parts": "Una categoría es de otro workspace."})
            if cat.type != txn.type:
                raise ValidationError(
                    {"parts": f"Una categoría no es de tipo «{txn.type}»."}
                )

        total = sum((p["amount"] for p in parts), Decimal("0"))
        if total != txn.amount:
            raise ValidationError(
                {"parts": f"Las partes suman {total}, pero la transacción es de {txn.amount}."}
            )

        group_id = uuid.uuid4()
        with db_transaction.atomic():
            new_parts = [
                Transaction.objects.create(
                    type=txn.type,
                    wallet=txn.wallet,
                    category=p["category"],
                    amount=p["amount"],
                    description=p.get("description") or txn.description,
                    date=txn.date,
                    counts_toward_budget=txn.counts_toward_budget,
                    created_by=txn.created_by,
                    source=txn.source,
                    split_group=group_id,
                )
                for p in parts
            ]
            txn.soft_delete()

        return Response(self.get_serializer(new_parts, many=True).data, status=201)


# ---------------------------------------------------------------------------
# CategoryBudget
# ---------------------------------------------------------------------------
class CategoryBudgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryBudget
        fields = ("id", "category", "amount", "month", "year", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_category(self, category):
        if category.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("La categoría es de otro workspace.")
        return category

    def validate_month(self, value):
        if not 1 <= value <= 12:
            raise serializers.ValidationError("El mes debe estar entre 1 y 12.")
        return value

    def validate(self, attrs):
        category = attrs.get("category") or getattr(self.instance, "category", None)
        month = attrs.get("month", getattr(self.instance, "month", None))
        year = attrs.get("year", getattr(self.instance, "year", None))
        qs = CategoryBudget.objects.filter(category=category, month=month, year=year)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "Ya existe un presupuesto para esa categoría en ese mes."
            )
        return attrs

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class CategoryBudgetViewSet(WorkspaceScopedViewSet):
    serializer_class = CategoryBudgetSerializer
    queryset = CategoryBudget.objects.select_related("workspace", "category").all()
    filterset_fields = {"month": ["exact"], "year": ["exact"], "category": ["exact"]}


# ---------------------------------------------------------------------------
# RecurringExpense
# ---------------------------------------------------------------------------
class RecurringExpenseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    workspace_child_fields = ("category", "wallet")

    class Meta:
        model = RecurringExpense
        fields = (
            "id", "category", "wallet", "amount", "frequency", "next_due_date",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class RecurringSuggestionSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=Transaction.TYPE_CHOICES)
    category = serializers.UUIDField()
    category_name = serializers.CharField()
    wallet = serializers.UUIDField()
    wallet_name = serializers.CharField()
    suggested_amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    occurrences = serializers.IntegerField()
    last_date = serializers.DateField()
    suggested_next_due_date = serializers.DateField()


class RecurringSuggestionDismissSerializer(serializers.Serializer):
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all())
    wallet = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all())
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)


class RecurringExpenseViewSet(WorkspaceScopedViewSet):
    serializer_class = RecurringExpenseSerializer
    queryset = RecurringExpense.objects.select_related(
        "workspace", "category", "wallet"
    ).all()

    @action(detail=False, methods=["get"])
    def suggestions(self, request):
        """Candidatas a recurrente detectadas en el historial -- ver
        `services.detect_recurring_candidates`. No crea nada: para eso,
        `POST /recurring-expenses/` con estos mismos datos."""
        data = services.detect_recurring_candidates(request.workspace, request.user)
        return Response(RecurringSuggestionSerializer(data, many=True).data)

    @action(detail=False, methods=["post"], url_path="dismiss-suggestion")
    def dismiss_suggestion(self, request):
        """"No, gracias" a una sugerencia -- no se le vuelve a mostrar."""
        serializer = RecurringSuggestionDismissSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cat = serializer.validated_data["category"]
        wallet = serializer.validated_data["wallet"]
        if cat.workspace_id != request.workspace.id or wallet.workspace_id != request.workspace.id:
            raise ValidationError("La categoría o la cartera son de otro workspace.")
        services.dismiss_recurring_suggestion(
            request.workspace, cat, wallet, serializer.validated_data["amount"]
        )
        return Response(status=204)


# ---------------------------------------------------------------------------
# InstallmentPurchase
# ---------------------------------------------------------------------------
class InstallmentPurchaseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    workspace_child_fields = ("category", "wallet", "payment_wallet")
    is_completed = serializers.BooleanField(read_only=True)
    is_credit_card = serializers.BooleanField(read_only=True)
    remaining_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = InstallmentPurchase
        fields = (
            "id", "wallet", "payment_wallet", "category", "description",
            "total_amount", "installment_amount", "installments_total",
            "installments_paid", "start_date", "is_completed", "is_credit_card",
            "remaining_amount", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "is_completed", "is_credit_card", "remaining_amount",
            "created_at", "updated_at",
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        payment_wallet = attrs.get("payment_wallet") or getattr(
            self.instance, "payment_wallet", None
        )
        wallet = attrs.get("wallet") or getattr(self.instance, "wallet", None)
        if payment_wallet is not None:
            if wallet is not None and payment_wallet.id == wallet.id:
                raise serializers.ValidationError(
                    {"payment_wallet": "Debe ser distinta de la tarjeta."}
                )
            # Compra con tarjeta: el contador arranca en 0 (el total se carga al
            # crear; las cuotas ya pagadas se registran pulsando "pagar").
            if self.instance is None:
                attrs["installments_paid"] = 0
        return attrs

    def create(self, validated_data):
        purchase = super().create(validated_data)
        if purchase.payment_wallet_id:
            from .services import post_initial_installment_charge

            request = self.context.get("request")
            post_initial_installment_charge(
                purchase, user=getattr(request, "user", None)
            )
        return purchase


class InstallmentPurchaseViewSet(WorkspaceScopedViewSet):
    serializer_class = InstallmentPurchaseSerializer
    queryset = InstallmentPurchase.objects.select_related(
        "workspace", "category", "wallet", "payment_wallet"
    ).all()

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        """Registra la siguiente cuota: crea la transacción y avanza el contador."""
        from .services import post_next_installment

        purchase = self.get_object()
        # El pago manual se registra con fecha de hoy (cuándo lo pagaste de
        # verdad), no con la fecha teórica del calendario de cuotas — eso lo
        # usa el job automático `post_due_installments`.
        txn = post_next_installment(
            purchase, user=request.user, on_date=timezone.localdate()
        )
        if txn is None:
            return Response(
                {"detail": "La compra ya está completa."}, status=400
            )
        purchase.refresh_from_db()
        return Response(self.get_serializer(purchase).data)
