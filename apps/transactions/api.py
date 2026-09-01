from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import serializers

from apps.accounts.models import Wallet
from apps.common.api import WorkspaceScopedSerializerMixin, WorkspaceScopedViewSet

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
    class Meta:
        model = Category
        fields = ("id", "name", "icon", "color", "type", "parent", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_parent(self, parent):
        if parent is not None and parent.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("La categoría padre es de otro workspace.")
        return parent

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class CategoryViewSet(WorkspaceScopedViewSet):
    serializer_class = CategorySerializer
    queryset = Category.objects.select_related("workspace", "parent").all()


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------
class TransactionSerializer(serializers.ModelSerializer):
    # `type` es opcional al escribir: si se omite en income/expense se deduce
    # de la categoría. Requerido para transferencias.
    type = serializers.ChoiceField(
        choices=Transaction.TYPE_CHOICES, required=False
    )

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
            "counts_toward_budget",
            "source",
            "is_recurring",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_by", "created_at", "updated_at")

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
            attrs["category"] = None
            attrs["to_wallet"] = to_wallet
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


class TransactionFilter(filters.FilterSet):
    """Filtros de querystring para la lista de transacciones.

    Ej.: ``?date_after=2026-08-01&date_before=2026-08-31&source=manual``
    """

    date_after = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_before = filters.DateFilter(field_name="date", lookup_expr="lte")

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


class RecurringExpenseViewSet(WorkspaceScopedViewSet):
    serializer_class = RecurringExpenseSerializer
    queryset = RecurringExpense.objects.select_related(
        "workspace", "category", "wallet"
    ).all()


# ---------------------------------------------------------------------------
# InstallmentPurchase
# ---------------------------------------------------------------------------
class InstallmentPurchaseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    workspace_child_fields = ("category", "wallet")
    is_completed = serializers.BooleanField(read_only=True)
    remaining_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = InstallmentPurchase
        fields = (
            "id", "wallet", "category", "description", "total_amount",
            "installment_amount", "installments_total", "installments_paid",
            "start_date", "is_completed", "remaining_amount",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "is_completed", "remaining_amount", "created_at", "updated_at",
        )


class InstallmentPurchaseViewSet(WorkspaceScopedViewSet):
    serializer_class = InstallmentPurchaseSerializer
    queryset = InstallmentPurchase.objects.select_related(
        "workspace", "category", "wallet"
    ).all()
