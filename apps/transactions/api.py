import datetime as dt
import io
import mimetypes
import uuid
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Case, Count, DecimalField, F, Max, Min, Q, Sum, When
from django.db.models.deletion import ProtectedError
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
from apps.loyalty.models import LoyaltyEarning, LoyaltyProgram

from . import services
from . import xlsx_import as xlsx
from .models import (
    Category,
    CategoryBudget,
    InstallmentPurchase,
    RecurringExpense,
    Tag,
    Transaction,
)

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def _visible_wallets(workspace, user):
    return Wallet.objects.filter(workspace=workspace).filter(
        Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user)
    )


def _reject_if_group_wallet(wallet, field):
    """Una cartera con hijas es puramente un contenedor -- su saldo mostrado
    es la suma de sus hijas, nunca uno propio (ver `Wallet.aggregated_balance`).
    Igual que un grupo de categoría, no se le puede cargar nada directamente:
    si tuviera transacciones propias además de las de sus hijas, ese saldo
    "propio" quedaría escondido del usuario en vez de sumado."""
    if wallet is not None and Wallet.objects.filter(parent_id=wallet.id).exists():
        raise serializers.ValidationError(
            {field: "Esta cartera agrupa a otras (tiene hijas); elegí una de sus hijas."}
        )


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------
class CategorySerializer(serializers.ModelSerializer):
    is_group = serializers.BooleanField(read_only=True)
    # Cantidad de transacciones vivas con esta categoría: la usa el cliente
    # para mostrar primero las más usadas al elegir categoría en una
    # transacción (ver `CategoryViewSet.get_queryset`). `default=0` cubre las
    # acciones que no la anotan (p. ej. `deleted`/`restore`).
    usage_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Category
        fields = (
            "id", "name", "icon", "color", "type", "parent", "sort_order",
            "category_type", "is_group", "usage_count", "created_at", "updated_at",
        )
        read_only_fields = ("id", "is_group", "usage_count", "created_at", "updated_at")

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

    def get_queryset(self):
        return super().get_queryset().annotate(
            usage_count=Count(
                "transactions", filter=Q(transactions__is_deleted=False), distinct=True
            )
        )

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

    @action(detail=True, methods=["delete"], url_path="purge")
    def purge(self, request, pk=None):
        """Borra DEFINITIVAMENTE una categoría ya eliminada (soft-delete) --
        para poder vaciar "Eliminadas", que si no se va acumulando para
        siempre. Sólo opera sobre lo que ya está soft-deleted, y sólo si no
        queda nada real colgando de ella (si no, 400 en vez de arrastrar un
        borrado en cascada silencioso sobre presupuestos/recurrentes)."""
        cat = Category.all_objects.filter(
            workspace=request.workspace, id=pk, is_deleted=True
        ).first()
        if cat is None:
            return Response({"detail": "No encontrada."}, status=404)
        if cat.subcategories.filter(is_deleted=False).exists():
            raise ValidationError("Tiene subcategorías activas; eliminalas primero.")
        if (
            cat.budgets.exists()
            or cat.recurring_expenses.exists()
            or cat.installment_purchases.exists()
        ):
            raise ValidationError(
                "Tiene presupuestos o recurrentes asociados; no se puede borrar del todo."
            )
        try:
            cat.delete()
        except ProtectedError:
            raise ValidationError("Tiene movimientos asociados; no se puede borrar del todo.")
        return Response(status=204)


# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------
class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ("id", "name", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Requerido.")
        workspace = self.context["workspace"]
        qs = Tag.objects.filter(workspace=workspace, name__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Ya existe una etiqueta con ese nombre.")
        return value

    def create(self, validated_data):
        validated_data["workspace"] = self.context["workspace"]
        return super().create(validated_data)


class TagSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    income = serializers.DecimalField(max_digits=16, decimal_places=2)
    expense = serializers.DecimalField(max_digits=16, decimal_places=2)
    count = serializers.IntegerField()
    first_date = serializers.DateField(allow_null=True)
    last_date = serializers.DateField(allow_null=True)


class TagViewSet(WorkspaceScopedViewSet):
    """Etiquetas libres del workspace. La asignación a transacciones va por
    `TransactionSerializer.tag_names` (get-or-create), no por acá -- esto es
    solo para listar, renombrar, borrar y ver el total de cada una."""

    serializer_class = TagSerializer
    queryset = Tag.objects.all()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Ingresos/gastos acumulados y cantidad de transacciones por
        etiqueta (visibles para `request.user`) -- "cuánto llevo gastado en
        el viaje X", sin importar en qué categoría cayó cada gasto."""
        visible = services.visible_transactions(request.workspace, request.user)
        rows = []
        for tag in Tag.objects.filter(workspace=request.workspace):
            agg = visible.filter(tags=tag).aggregate(
                income=Sum(
                    Case(When(type=Transaction.TYPE_INCOME, then=F("amount")), default=Decimal("0"), output_field=_MONEY)
                ),
                expense=Sum(
                    Case(When(type=Transaction.TYPE_EXPENSE, then=F("amount")), default=Decimal("0"), output_field=_MONEY)
                ),
                count=Count("id"),
                first_date=Min("date"),
                last_date=Max("date"),
            )
            rows.append(
                {
                    "id": tag.id,
                    "name": tag.name,
                    "income": agg["income"] or Decimal("0"),
                    "expense": agg["expense"] or Decimal("0"),
                    "count": agg["count"] or 0,
                    "first_date": agg["first_date"],
                    "last_date": agg["last_date"],
                }
            )
        rows.sort(key=lambda r: (r["expense"], r["income"]), reverse=True)
        return Response(TagSummarySerializer(rows, many=True).data)


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
    tags = TagSerializer(many=True, read_only=True)
    # Nombres de etiqueta tal como los escribe el usuario: si ya existe una
    # con ese nombre en el workspace (sin distinguir mayúsculas) se reusa, si
    # no se crea al vuelo -- no hace falta un paso previo de "crear etiqueta".
    tag_names = serializers.ListField(
        child=serializers.CharField(max_length=40), write_only=True, required=False
    )
    # Descuento sugerido aplicado a mano por el cliente (ver
    # `IMPORTANT`/docstring de la clase): sólo se manda si el usuario tocó el
    # botón "aplicar descuento" del formulario. Van los dos juntos o ninguno.
    discount_program = serializers.PrimaryKeyRelatedField(
        queryset=LoyaltyProgram.objects.filter(kind=LoyaltyProgram.KIND_DISCOUNT, is_active=True),
        write_only=True, required=False, allow_null=True,
    )
    pre_discount_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, write_only=True, required=False, allow_null=True,
    )
    loyalty_earnings = serializers.SerializerMethodField()

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
            "tags",
            "tag_names",
            "discount_program",
            "pre_discount_amount",
            "loyalty_earnings",
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

    def get_loyalty_earnings(self, obj) -> list[dict]:
        """Puntos/cashback/descuento que generó esta transacción -- de sólo
        lectura, ver `apps.loyalty`."""
        return [
            {
                "kind": e.kind,
                "program": str(e.program_id),
                "program_name": e.program.name,
                "points": e.points,
                "amount": e.amount,
                "original_amount": e.original_amount,
                "saved_amount": e.discount_saved_amount,
            }
            for e in obj.loyalty_earnings.select_related("program").all()
        ]

    def validate_tag_names(self, value):
        if len(value) > 8:
            raise serializers.ValidationError("Máximo 8 etiquetas por transacción.")
        return value

    def _resolve_tags(self, names):
        """Get-or-create, sin duplicar por mayúsculas/espacios."""
        workspace = self.context["workspace"]
        result = []
        seen = set()
        for raw in names:
            name = raw.strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            tag = Tag.objects.filter(workspace=workspace, name__iexact=name).first()
            if tag is None:
                tag = Tag.objects.create(workspace=workspace, name=name)
            result.append(tag)
        return result

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
        _reject_if_group_wallet(wallet, field)

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

        discount_program = attrs.get("discount_program")
        pre_discount_amount = attrs.get("pre_discount_amount")
        if discount_program is not None or pre_discount_amount is not None:
            if discount_program is None or pre_discount_amount is None:
                raise serializers.ValidationError(
                    {"discount_program": "Mandá el programa y el monto original juntos, o ninguno."}
                )
            if wallet is not None and discount_program.card_product_id != wallet.card_product_id:
                raise serializers.ValidationError(
                    {"discount_program": "No corresponde a la tarjeta de esta transacción."}
                )
            amount = attrs.get("amount", getattr(inst, "amount", None))
            if amount is not None and pre_discount_amount < amount:
                raise serializers.ValidationError(
                    {"pre_discount_amount": "Tiene que ser mayor o igual al monto final."}
                )

        return attrs

    def _sync_discount(self, instance, discount_program, pre_discount_amount):
        if discount_program is None and pre_discount_amount is None:
            return
        LoyaltyEarning.objects.update_or_create(
            transaction=instance,
            program=discount_program,
            defaults={
                "workspace": self.context["workspace"],
                "kind": LoyaltyProgram.KIND_DISCOUNT,
                "original_amount": pre_discount_amount,
            },
        )

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        tag_names = validated_data.pop("tag_names", None)
        discount_program = validated_data.pop("discount_program", None)
        pre_discount_amount = validated_data.pop("pre_discount_amount", None)
        instance = super().create(validated_data)
        if tag_names is not None:
            instance.tags.set(self._resolve_tags(tag_names))
        self._sync_discount(instance, discount_program, pre_discount_amount)
        return instance

    def update(self, instance, validated_data):
        tag_names = validated_data.pop("tag_names", None)
        discount_program = validated_data.pop("discount_program", None)
        pre_discount_amount = validated_data.pop("pre_discount_amount", None)
        instance = super().update(instance, validated_data)
        if tag_names is not None:
            instance.tags.set(self._resolve_tags(tag_names))
        self._sync_discount(instance, discount_program, pre_discount_amount)
        return instance


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
    tag = filters.UUIDFilter(field_name="tags__id")
    search = filters.CharFilter(method="filter_search")
    # `wallet` matchea tanto si la cartera es el origen como si es el destino
    # de una transferencia -- si no, el historial de una cartera que solo
    # recibe transferencias (nunca aparece como `wallet` en esas filas) se
    # ve vacío pese a tener saldo. `to_wallet` (abajo, en Meta.fields) queda
    # disponible aparte para quien de verdad quiera solo "las que entran".
    wallet = filters.UUIDFilter(method="filter_wallet")

    def filter_wallet(self, queryset, name, value):
        return queryset.filter(Q(wallet=value) | Q(to_wallet=value))

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
    ).prefetch_related("loyalty_earnings__program").all()

    def get_queryset(self):
        user = self.request.user
        return super().get_queryset().filter(
            Q(wallet__visibility=Wallet.VISIBILITY_SHARED) | Q(wallet__owner=user)
        )

    RECEIPT_MAX_SIZE = 8 * 1024 * 1024  # 8 MB
    RECEIPT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
    IMPORT_MAX_SIZE = 5 * 1024 * 1024  # 5 MB -- de sobra para miles de filas de texto

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
        original_tags = list(txn.tags.all())
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
            if original_tags:
                for part in new_parts:
                    part.tags.set(original_tags)
            txn.soft_delete()

        return Response(self.get_serializer(new_parts, many=True).data, status=201)

    @action(detail=False, methods=["get"], url_path="import-template")
    def import_template(self, request):
        """Plantilla .xlsx para cargar transacciones en lote (ver acción
        `import`), con los nombres reales de carteras/categorías del
        workspace en hojas de referencia."""
        wallets = _visible_wallets(request.workspace, request.user).filter(is_archived=False)
        categories = Category.objects.filter(workspace=request.workspace)
        content = xlsx.build_template(wallets, categories)
        response = FileResponse(
            io.BytesIO(content),
            as_attachment=True,
            filename="plantilla-transacciones.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return response

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_xlsx(self, request):
        """Crea transacciones en lote desde el .xlsx de `import-template`
        (ya editado por el usuario). Es "todo lo que se pueda": cada fila
        se valida por su cuenta con el mismo `TransactionSerializer` de
        siempre, así que una fila con error no frena a las demás -- se
        crean las válidas y se reportan los errores fila por fila."""
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"file": "Requerido."})
        if file.size > self.IMPORT_MAX_SIZE:
            raise ValidationError({"file": "El archivo pesa más de 5 MB."})

        try:
            rows = xlsx.parse_workbook(file)
        except xlsx.InvalidWorkbook:
            raise ValidationError(
                {"file": "No se pudo leer el archivo -- ¿es un .xlsx válido descargado desde acá?"}
            )

        wallets_by_name = xlsx.index_wallets_by_name(
            _visible_wallets(request.workspace, request.user).filter(is_archived=False)
        )
        categories_by_name = xlsx.index_categories_by_name(
            Category.objects.filter(workspace=request.workspace)
        )

        context = self.get_serializer_context()
        created = []
        errors = []
        with db_transaction.atomic():
            for row_number, values in rows:
                try:
                    payload = xlsx.row_to_payload(values, wallets_by_name, categories_by_name)
                except xlsx.RowError as exc:
                    errors.append({"row": row_number, "message": str(exc)})
                    continue
                serializer = TransactionSerializer(data=payload, context=context)
                if not serializer.is_valid():
                    message = "; ".join(
                        f"{field}: {', '.join(str(m) for m in msgs)}"
                        for field, msgs in serializer.errors.items()
                    )
                    errors.append({"row": row_number, "message": message})
                    continue
                created.append(serializer.save())

        return Response(
            {
                "created": len(created),
                "errors": errors,
                "transactions": TransactionSerializer(created, many=True, context=context).data,
            },
            status=200,
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
        # Un grupo SIN subcategorías puede presupuestarse directamente (es su
        # propia unidad, como cualquier categoría hoja). Uno CON subcategorías
        # no: su presupuesto es la suma de las suyas, no algo aparte.
        if category.parent_id is None and category.subcategories.filter(is_deleted=False).exists():
            raise serializers.ValidationError(
                "Este grupo tiene subcategorías; presupuéstalas a ellas en vez de al grupo."
            )
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


class SetForwardBudgetSerializer(serializers.Serializer):
    category = serializers.PrimaryKeyRelatedField(queryset=Category.objects.all())
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    month = serializers.IntegerField(min_value=1, max_value=12)
    year = serializers.IntegerField(min_value=2000, max_value=2100)


class CategoryBudgetViewSet(WorkspaceScopedViewSet):
    serializer_class = CategoryBudgetSerializer
    queryset = CategoryBudget.objects.select_related("workspace", "category").all()
    filterset_fields = {"month": ["exact"], "year": ["exact"], "category": ["exact"]}

    # Cuántos meses hacia adelante puede materializar `set_forward` como
    # máximo en una sola llamada, para no crear filas indefinidamente si el
    # usuario nunca vuelve a tocar esa categoría (3 años de margen).
    FORWARD_HORIZON_MONTHS = 36

    @action(detail=False, methods=["post"], url_path="set-forward")
    def set_forward(self, request):
        """
        Fija el presupuesto de una categoría para un mes y lo propaga a los
        meses siguientes: cada mes futuro que no tenía presupuesto propio, o
        que coincidía con el monto anterior de este mes, se actualiza al
        nuevo monto -- hasta el primer mes que el usuario ya haya
        personalizado con un valor distinto, donde se corta la propagación.
        Los meses anteriores nunca se tocan, así se conserva el histórico.
        """
        serializer = SetForwardBudgetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.validated_data["category"]
        if category.workspace_id != request.workspace.id:
            raise ValidationError({"category": "La categoría es de otro workspace."})
        if category.parent_id is None and category.subcategories.filter(is_deleted=False).exists():
            raise ValidationError(
                {"category": "Este grupo tiene subcategorías; presupuéstalas a ellas en vez de al grupo."}
            )
        amount = serializer.validated_data["amount"]
        month = serializer.validated_data["month"]
        year = serializer.validated_data["year"]

        current = CategoryBudget.objects.filter(
            category=category, month=month, year=year
        ).first()
        old_amount = current.amount if current else None
        if current:
            current.amount = amount
            current.save(update_fields=["amount"])
        else:
            current = CategoryBudget.objects.create(
                workspace=request.workspace, category=category,
                month=month, year=year, amount=amount,
            )

        months_touched = 1
        cm, cy = month, year
        for _ in range(self.FORWARD_HORIZON_MONTHS):
            cm += 1
            if cm > 12:
                cm = 1
                cy += 1
            future = CategoryBudget.objects.filter(category=category, month=cm, year=cy).first()
            if future is None:
                CategoryBudget.objects.create(
                    workspace=request.workspace, category=category,
                    month=cm, year=cy, amount=amount,
                )
                months_touched += 1
            elif old_amount is not None and future.amount == old_amount:
                future.amount = amount
                future.save(update_fields=["amount"])
                months_touched += 1
            else:
                break  # mes ya personalizado por el usuario: no seguimos

        data = CategoryBudgetSerializer(current).data
        data["months_touched"] = months_touched
        return Response(data)


# ---------------------------------------------------------------------------
# RecurringExpense
# ---------------------------------------------------------------------------
class RecurringExpenseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    workspace_child_fields = ("category", "wallet")

    class Meta:
        model = RecurringExpense
        fields = (
            "id", "name", "category", "wallet", "amount", "frequency", "next_due_date",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        wallet = attrs.get("wallet") or getattr(self.instance, "wallet", None)
        _reject_if_group_wallet(wallet, "wallet")
        next_due = attrs.get("next_due_date")
        current = getattr(self.instance, "next_due_date", None)
        # Sólo rechaza si de verdad está ELIGIENDO una fecha pasada nueva --
        # si no cambió (p. ej. ya estaba vencido y sólo se edita el monto),
        # no bloquea: eso lo resuelve el job de generación, no este form.
        if next_due is not None and next_due != current and next_due < timezone.localdate():
            raise serializers.ValidationError(
                {"next_due_date": "No puede ser una fecha pasada."}
            )
        return attrs


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
def _installment_txn_description(purchase) -> str:
    return f"{purchase.description} (compra a {purchase.installments_total} cuotas)"


class InstallmentPurchaseSerializer(WorkspaceScopedSerializerMixin, serializers.ModelSerializer):
    """Una compra a plazo genera UNA sola Transaction (el total, contra su
    `wallet`, el día `start_date`) al crearla -- ver `create`/`update` acá
    abajo. Los campos de cuotas (`installments_paid`, `remaining_amount`,
    etc.) son puro cálculo de solo lectura, anclado a los cortes de `wallet`
    (ver `apps.accounts.services.installment_status`); no hay ningún
    contador que el cliente pueda mover a mano.
    """
    workspace_child_fields = ("category", "wallet")
    installments_paid = serializers.SerializerMethodField()
    is_completed = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    current_installment_amount = serializers.SerializerMethodField()
    next_due_date = serializers.SerializerMethodField()

    class Meta:
        model = InstallmentPurchase
        fields = (
            "id", "wallet", "category", "description", "total_amount",
            "installments_total", "start_date", "installments_paid",
            "is_completed", "remaining_amount", "current_installment_amount",
            "next_due_date", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "installments_paid", "is_completed", "remaining_amount",
            "current_installment_amount", "next_due_date",
            "created_at", "updated_at",
        )

    def _status(self, obj):
        from apps.accounts.services import installment_status

        cached = getattr(obj, "_installment_status_cache", None)
        if cached is None:
            cached = installment_status(obj)
            obj._installment_status_cache = cached
        return cached

    def get_installments_paid(self, obj) -> int:
        return self._status(obj)["installments_paid"]

    def get_is_completed(self, obj) -> bool:
        return self._status(obj)["is_completed"]

    def get_remaining_amount(self, obj) -> Decimal:
        return self._status(obj)["remaining_amount"]

    def get_current_installment_amount(self, obj) -> Decimal:
        return self._status(obj)["current_installment_amount"]

    def get_next_due_date(self, obj) -> dt.date | None:
        return self._status(obj)["next_due_date"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        wallet = attrs.get("wallet") or getattr(self.instance, "wallet", None)
        _reject_if_group_wallet(wallet, "wallet")
        if wallet is not None and (
            wallet.kind != Wallet.KIND_CREDIT or not wallet.billing_cycle_day
        ):
            raise serializers.ValidationError(
                {"wallet": "Elegí una tarjeta de crédito con fecha de corte configurada."}
            )
        return attrs

    def create(self, validated_data):
        purchase = super().create(validated_data)
        request = self.context.get("request")
        Transaction.objects.create(
            wallet=purchase.wallet,
            category=purchase.category,
            amount=purchase.total_amount,
            description=_installment_txn_description(purchase),
            date=purchase.start_date,
            source=Transaction.SOURCE_INSTALLMENT,
            installment_purchase=purchase,
            created_by=getattr(request, "user", None),
        )
        return purchase

    def update(self, instance, validated_data):
        purchase = super().update(instance, validated_data)
        # Mantiene la única Transaction de la compra en sincro con los
        # cambios del formulario (monto, cartera, categoría, fecha…) -- si
        # no, editar la compra dejaría el saldo/reporte reflejando los datos
        # viejos.
        txn = purchase.transactions.filter(is_deleted=False).first()
        if txn is not None:
            txn.wallet = purchase.wallet
            txn.category = purchase.category
            txn.amount = purchase.total_amount
            txn.date = purchase.start_date
            txn.description = _installment_txn_description(purchase)
            txn.save(
                update_fields=[
                    "wallet", "category", "amount", "date", "description",
                    "currency", "updated_at",
                ]
            )
        return purchase


class InstallmentPurchaseViewSet(WorkspaceScopedViewSet):
    serializer_class = InstallmentPurchaseSerializer
    queryset = InstallmentPurchase.objects.select_related(
        "workspace", "category", "wallet"
    ).all()

    def perform_destroy(self, instance):
        # La compra y su única Transaction son, en la práctica, un mismo
        # registro -- borrar la compra borra (soft-delete) esa transacción
        # también, para que el saldo de la tarjeta refleje el cambio.
        for txn in instance.transactions.filter(is_deleted=False):
            txn.soft_delete()
        super().perform_destroy(instance)
