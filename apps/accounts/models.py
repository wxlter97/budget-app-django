from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class Account(BaseModel):
    """Una cartera: banco, efectivo, tarjeta de credito, ahorro."""

    TYPE_CHECKING = "checking"
    TYPE_SAVINGS = "savings"
    TYPE_CREDIT = "credit"
    TYPE_CASH = "cash"
    TYPE_CHOICES = [
        (TYPE_CHECKING, "Cuenta corriente"),
        (TYPE_SAVINGS, "Ahorro"),
        (TYPE_CREDIT, "Tarjeta de credito"),
        (TYPE_CASH, "Efectivo"),
    ]

    VISIBILITY_SHARED = "shared"
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_CHOICES = [
        (VISIBILITY_SHARED, "Compartida"),
        (VISIBILITY_PRIVATE, "Privada"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="accounts")
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    currency = models.CharField(max_length=3, default="USD")

    # Saldo con el que se da de alta la cuenta (punto de partida fijo).
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Saldo actual = opening_balance + suma de transacciones. Se mantiene por
    # signals al crear/editar/borrar Transaction; recalculable con
    # `manage.py recompute_balances` si hiciera falta reconciliar.
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_SHARED)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="owned_accounts",
        help_text="Solo aplica si visibility=private",
    )

    # Solo para tarjetas -- nunca se guarda el numero completo.
    card_last4 = models.CharField(max_length=4, blank=True, null=True)
    billing_cycle_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Dia del mes de fecha de corte (tarjetas)"
    )
    payment_due_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Dia del mes de fecha limite de pago (tarjetas)"
    )

    is_active = models.BooleanField(default=True)
    # Cuenta preseleccionada al crear una transacción. Máx. una por workspace.
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_default=True, is_deleted=False),
                name="one_default_account_per_workspace",
            ),
        ]

    def save(self, *args, **kwargs):
        # Al crear la cuenta, el saldo actual arranca en el saldo de apertura.
        if self._state.adding and not self.current_balance:
            self.current_balance = self.opening_balance
        # Solo una cuenta default por workspace: desmarca las demás antes de guardar.
        if self.is_default and self.workspace_id:
            Account.objects.filter(
                workspace_id=self.workspace_id, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.workspace})"


class Asset(BaseModel):
    """Patrimonio que no es una cuenta liquida: propiedad, vehiculo, inversion."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="assets")
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=50)
    current_value = models.DecimalField(max_digits=14, decimal_places=2)
    visibility = models.CharField(
        max_length=10, choices=Account.VISIBILITY_CHOICES, default=Account.VISIBILITY_SHARED
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="owned_assets",
    )

    def __str__(self):
        return self.name


class Liability(BaseModel):
    """Prestamo, hipoteca u otro pasivo con saldo pendiente."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="liabilities")
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=50)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2)
    remaining_amount = models.DecimalField(max_digits=14, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name


class Debt(BaseModel):
    """Deuda personal fuera del sistema bancario: a favor (te deben) o en contra (debes)."""

    DIRECTION_FAVOR = "a_favor"
    DIRECTION_CONTRA = "en_contra"
    DIRECTION_CHOICES = [(DIRECTION_FAVOR, "A favor"), (DIRECTION_CONTRA, "En contra")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="debts")
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    person = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    is_settled = models.BooleanField(default=False)

    def __str__(self):
        sign = "+" if self.direction == self.DIRECTION_FAVOR else "-"
        return f"{self.person}: {sign}{self.amount}"
