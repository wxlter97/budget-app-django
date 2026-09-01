from django.conf import settings
from django.db import models

from apps.accounts.models import Account
from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class Category(BaseModel):
    TYPE_INCOME = "income"
    TYPE_EXPENSE = "expense"
    TYPE_CHOICES = [(TYPE_INCOME, "Ingreso"), (TYPE_EXPENSE, "Gasto")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)
    icon = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=20, blank=True)
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="subcategories"
    )

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
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_EMAIL_IMPORT, "Importada por correo"),
        (SOURCE_RECURRING, "Gasto recurrente"),
        (SOURCE_INSTALLMENT, "Cuota de compra a plazo"),
    ]

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    # Cuenta origen. En transferencias, de aquí sale el dinero.
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    # Solo transferencias: cuenta destino (a la que entra el dinero).
    to_account = models.ForeignKey(
        Account,
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
            self.category = None
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
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_YEARLY = "yearly"
    FREQUENCY_CHOICES = [(FREQUENCY_MONTHLY, "Mensual"), (FREQUENCY_YEARLY, "Anual")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="recurring_expenses")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="recurring_expenses")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="recurring_expenses")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default=FREQUENCY_MONTHLY)
    next_due_date = models.DateField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.category} recurrente {self.amount}/{self.frequency}"


class InstallmentPurchase(BaseModel):
    """Compra a plazo: genera una Transaction por cuota mensual."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="installment_purchases")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="installment_purchases")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="installment_purchases")
    description = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    installment_amount = models.DecimalField(max_digits=14, decimal_places=2)
    installments_total = models.PositiveSmallIntegerField()
    installments_paid = models.PositiveSmallIntegerField(default=0)
    start_date = models.DateField()

    @property
    def is_completed(self):
        return self.installments_paid >= self.installments_total

    @property
    def remaining_amount(self):
        return self.installment_amount * (self.installments_total - self.installments_paid)

    def __str__(self):
        return f"{self.description} ({self.installments_paid}/{self.installments_total})"
