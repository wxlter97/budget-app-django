from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import serializers

from apps.accounts.models import Account
from apps.common.api import WorkspaceScopedSerializerMixin, WorkspaceScopedViewSet

from .models import (
    Category,
    CategoryBudget,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)


def _visible_accounts(workspace, user):
    return Account.objects.filter(workspace=workspace).filter(
        Q(visibility=Account.VISIBILITY_SHARED) | Q(owner=user)
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
            "account",
            "to_account",
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

    def _check_account(self, account, workspace, user, field):
        if account is None:
            return
        if account.workspace_id != workspace.id:
            raise serializers.ValidationError({field: "La cuenta es de otro workspace."})
        if (
            account.visibility == Account.VISIBILITY_PRIVATE
            and account.owner_id != user.id
        ):
            raise serializers.ValidationError(
                {field: "No puedes usar una cuenta privada ajena."}
            )

    def validate(self, attrs):
        workspace = self.context["workspace"]
        user = self.context["request"].user
        inst = self.instance

        account = attrs.get("account") or getattr(inst, "account", None)
        to_account = attrs.get("to_account", getattr(inst, "to_account", None))
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

        self._check_account(account, workspace, user, "account")

        if txn_type == Transaction.TYPE_TRANSFER:
            if to_account is None:
                raise serializers.ValidationError(
                    {"to_account": "Requerida en una transferencia."}
                )
            if account is not None and to_account.id == account.id:
                raise serializers.ValidationError(
                    {"to_account": "La cuenta destino debe ser distinta de la origen."}
                )
            self._check_account(to_account, workspace, user, "to_account")
            attrs["category"] = None
            attrs["to_account"] = to_account
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
            attrs["to_account"] = None

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
            "account": ["exact"],
            "to_account": ["exact"],
            "category": ["exact"],
            "source": ["exact"],
            "is_recurring": ["exact"],
            "counts_toward_budget": ["exact"],
        }


class TransactionViewSet(WorkspaceScopedViewSet):
    """Transacciones del workspace activo (scoping vía account__workspace)."""

    serializer_class = TransactionSerializer
    workspace_field = "account__workspace"
    filterset_class = TransactionFilter
    queryset = Transaction.objects.select_related(
        "account", "to_account", "category", "created_by"
    ).all()

    def get_queryset(self):
        user = self.request.user
        return super().get_queryset().filter(
            Q(account__visibility=Account.VISIBILITY_SHARED) | Q(account__owner=user)
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
    workspace_child_fields = ("category", "account")

    class Meta:
        model = RecurringExpense
        fields = (
            "id", "category", "account", "amount", "frequency", "next_due_date",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class RecurringExpenseViewSet(WorkspaceScopedViewSet):
    serializer_class = RecurringExpenseSerializer
    queryset = RecurringExpense.objects.select_related(
        "workspace", "category", "account"
    ).all()


# ---------------------------------------------------------------------------
# InstallmentPurchase
# ---------------------------------------------------------------------------
class InstallmentPurchaseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    workspace_child_fields = ("category", "account")
    is_completed = serializers.BooleanField(read_only=True)
    remaining_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = InstallmentPurchase
        fields = (
            "id", "account", "category", "description", "total_amount",
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
        "workspace", "category", "account"
    ).all()
