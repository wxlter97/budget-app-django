from django.conf import settings
from django.db import models

from apps.accounts.models import Wallet
from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


def receipt_upload_path(instance: "Transaction", filename: str) -> str:
    """Namespaced por workspace y transacción, para no pisar archivos entre
    workspaces y para poder borrar/reemplazar sin ambigüedad."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"receipts/{instance.wallet.workspace_id}/{instance.id}.{ext}"


class Category(BaseModel):
    TYPE_INCOME = "income"
    TYPE_EXPENSE = "expense"
    TYPE_CHOICES = [(TYPE_INCOME, "Ingreso"), (TYPE_EXPENSE, "Gasto")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)
    icon = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=20, blank=True)
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    # Jerarquía de 2 niveles estilo Buddy: una categoría sin `parent` es un
    # GRUPO (bucket de presupuesto, p. ej. "Vivienda"); con `parent` es una
    # categoría asignable a transacciones. El `parent` debe ser siempre un
    # grupo (nunca otra subcategoría) — lo valida el serializer.
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="subcategories"
    )
    # Orden manual dentro del grupo (o entre grupos si es grupo).
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    @property
    def is_group(self) -> bool:
        return self.parent_id is None

    def __str__(self):
        return self.name


class Transaction(BaseModel):
    TYPE_INCOME = "income"
    TYPE_EXPENSE = "expense"
    TYPE_TRANSFER = "transfer"
    TYPE_CHOICES = [
        (TYPE_INCOME, "Ingreso"),
        (TYPE_EXPENSE, "Gasto"),
        (TYPE_TRANSFER, "Transferencia"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_EMAIL_IMPORT = "email_import"
    SOURCE_RECURRING = "recurring"
    SOURCE_INSTALLMENT = "installment"
    SOURCE_QUICK_ADD = "quick_add"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_EMAIL_IMPORT, "Importada por correo"),
        (SOURCE_RECURRING, "Gasto recurrente"),
        (SOURCE_INSTALLMENT, "Cuota de compra a plazo"),
        (SOURCE_QUICK_ADD, "Alta rápida (Atajo)"),
    ]

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    # Cartera origen. En transferencias, de aquí sale el dinero.
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    # Solo transferencias: cartera destino (a la que entra el dinero).
    to_wallet = models.ForeignKey(
        Wallet,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="incoming_transfers",
    )
    # Requerida en income/expense; nula en transferencias.
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    description = models.CharField(max_length=255, blank=True)
    date = models.DateField()
    # Foto del recibo/comprobante (opcional). Se sube y se lee por
    # `/transactions/{id}/receipt/`, nunca por una URL directa del storage
    # (ver STORAGES en settings) — así el archivo queda protegido por la
    # misma membresía de workspace que el resto del API.
    receipt = models.ImageField(upload_to=receipt_upload_path, null=True, blank=True)
    # Si es False, el gasto no cuenta contra el presupuesto de su categoría
    # (sigue afectando el saldo y el resumen de gastos del mes).
    counts_toward_budget = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="transactions"
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    is_recurring = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=["income", "expense", "transfer"]),
                name="transaction_type_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.type == self.TYPE_TRANSFER:
            # Una transferencia puede llevar categoría opcional (p. ej. mover
            # dinero a "Ahorro"): si la tiene y `counts_toward_budget`, cuenta
            # contra el presupuesto de esa categoría/grupo. Sin categoría no
            # cuenta nunca.
            if self.category_id is None:
                self.counts_toward_budget = False
        elif self.category_id:
            # income / expense: el tipo lo manda la categoría (así, editar la
            # categoría de una transacción cambia su efecto sobre el saldo).
            self.type = self.category.type
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} {self.amount}"


class CategoryBudget(BaseModel):
    """El monto presupuestado para una categoria en un mes especifico."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="category_budgets")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="budgets")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "month", "year"], name="unique_budget_per_category_month"
            )
        ]

    def __str__(self):
        return f"{self.category} {self.month}/{self.year}: {self.amount}"


class CategoryProvision(BaseModel):
    """
    Acumulado de sobrante de una categoria que rueda mes a mes en vez de
    perderse. Cuando el gasto real del mes es menor al CategoryBudget,
    la diferencia se suma aqui (via tarea programada de cierre de mes).
    """
    category = models.OneToOneField(Category, on_delete=models.CASCADE, related_name="provision")
    accumulated_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    last_updated = models.DateField(auto_now=True)

    def __str__(self):
        return f"Provision {self.category}: {self.accumulated_amount}"


class RecurringExpense(BaseModel):
    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_BIWEEKLY = "biweekly"
    FREQUENCY_EVERY_3_WEEKS = "every_3_weeks"
    FREQUENCY_EVERY_4_WEEKS = "every_4_weeks"
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_EVERY_2_MONTHS = "every_2_months"
    FREQUENCY_EVERY_3_MONTHS = "every_3_months"
    FREQUENCY_EVERY_4_MONTHS = "every_4_months"
    FREQUENCY_EVERY_6_MONTHS = "every_6_months"
    FREQUENCY_YEARLY = "yearly"
    FREQUENCY_CHOICES = [
        (FREQUENCY_WEEKLY, "Cada semana"),
        (FREQUENCY_BIWEEKLY, "Cada dos semanas"),
        (FREQUENCY_EVERY_3_WEEKS, "Cada tres semanas"),
        (FREQUENCY_EVERY_4_WEEKS, "Cada cuatro semanas"),
        (FREQUENCY_MONTHLY, "Cada mes"),
        (FREQUENCY_EVERY_2_MONTHS, "Cada dos meses"),
        (FREQUENCY_EVERY_3_MONTHS, "Cada tres meses"),
        (FREQUENCY_EVERY_4_MONTHS, "Cada cuatro meses"),
        (FREQUENCY_EVERY_6_MONTHS, "Cada seis meses"),
        (FREQUENCY_YEARLY, "Cada año"),
    ]
    # (frecuencia -> kwargs para dateutil.relativedelta)
    FREQUENCY_DELTAS = {
        FREQUENCY_WEEKLY: {"weeks": 1},
        FREQUENCY_BIWEEKLY: {"weeks": 2},
        FREQUENCY_EVERY_3_WEEKS: {"weeks": 3},
        FREQUENCY_EVERY_4_WEEKS: {"weeks": 4},
        FREQUENCY_MONTHLY: {"months": 1},
        FREQUENCY_EVERY_2_MONTHS: {"months": 2},
        FREQUENCY_EVERY_3_MONTHS: {"months": 3},
        FREQUENCY_EVERY_4_MONTHS: {"months": 4},
        FREQUENCY_EVERY_6_MONTHS: {"months": 6},
        FREQUENCY_YEARLY: {"years": 1},
    }

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="recurring_expenses")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="recurring_expenses")
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="recurring_expenses")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES, default=FREQUENCY_MONTHLY)
    next_due_date = models.DateField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.category} recurrente {self.amount}/{self.frequency}"


class InstallmentPurchase(BaseModel):
    """Compra a plazo: genera una Transaction por cuota mensual.

    Dos modelos según `payment_wallet`:

    - **Sin `payment_wallet`** (plan de tienda / débito): no se registra nada al
      crear; cada cuota es un GASTO de `installment_amount` contra `wallet`.
    - **Con `payment_wallet`** (tarjeta de crédito): al crear se carga el
      `total_amount` completo como gasto contra `wallet` (la tarjeta) — ya debes
      todo y baja tu disponible; cada cuota es una TRANSFERENCIA de
      `installment_amount` desde `payment_wallet` hacia `wallet`.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="installment_purchases")
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="installment_purchases")
    # Tarjeta de crédito: cartera desde la que se pagan las cuotas. Si se define,
    # el total se carga a `wallet` al crear y cada cuota es una transferencia.
    payment_wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="installment_payments",
    )
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="installment_purchases")
    description = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    installment_amount = models.DecimalField(max_digits=14, decimal_places=2)
    installments_total = models.PositiveSmallIntegerField()
    installments_paid = models.PositiveSmallIntegerField(default=0)
    start_date = models.DateField()

    @property
    def is_credit_card(self) -> bool:
        return self.payment_wallet_id is not None

    @property
    def is_completed(self):
        return self.installments_paid >= self.installments_total

    @property
    def remaining_amount(self):
        return self.installment_amount * (self.installments_total - self.installments_paid)

    def __str__(self):
        return f"{self.description} ({self.installments_paid}/{self.installments_total})"
