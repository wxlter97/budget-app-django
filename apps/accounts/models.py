"""
Modelo `Wallet` (cartera): unifica lo que antes eran `Account`, `SavingsGoal`,
`ReserveFund`, `Asset`, `Liability` y `Debt`.

La app sigue llamándose `accounts` por compatibilidad de `app_label` en las
migraciones; el modelo y la API son `Wallet` / `/api/v1/wallets/`.
"""
from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class Wallet(BaseModel):
    """
    Una cartera: banco, efectivo, tarjeta, ahorro con meta, activo, deuda...
    El `purpose` dice para qué es; el saldo (`current_balance`) lo mantienen los
    signals de Transaction para las carteras con movimientos, o se fija a mano
    vía `opening_balance` para las que no (activos, préstamos, buckets de ahorro).
    """

    PURPOSE_SPENDING = "spending"   # gasto (banco, efectivo)
    PURPOSE_SAVINGS = "savings"     # ahorro (con meta o aportación mensual)
    PURPOSE_DEBT = "debt"           # deuda: saldo negativo = lo que debes
    PURPOSE_ASSET = "asset"         # activo: propiedad, vehículo, inversión
    PURPOSE_CHOICES = [
        (PURPOSE_SPENDING, "Gasto"),
        (PURPOSE_SAVINGS, "Ahorro"),
        (PURPOSE_DEBT, "Deuda"),
        (PURPOSE_ASSET, "Activo"),
    ]

    # Subtipo dentro del `purpose` (sobre todo para iconografía y para saber si
    # aplica `credit_limit`). Estilo Buddy: Gastos / Crédito / Efectivo / Personalizada.
    KIND_BANK = "bank"
    KIND_CREDIT = "credit"
    KIND_CASH = "cash"
    KIND_CUSTOM = "custom"
    KIND_CHOICES = [
        (KIND_BANK, "Cuenta bancaria"),
        (KIND_CREDIT, "Crédito"),
        (KIND_CASH, "Efectivo"),
        (KIND_CUSTOM, "Personalizada"),
    ]

    VISIBILITY_SHARED = "shared"
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_CHOICES = [
        (VISIBILITY_SHARED, "Compartida"),
        (VISIBILITY_PRIVATE, "Privada"),
    ]

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="wallets"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="Cartera padre; el saldo mostrado del padre incluye el de sus hijos.",
    )
    name = models.CharField(max_length=100)
    purpose = models.CharField(
        max_length=10, choices=PURPOSE_CHOICES, default=PURPOSE_SPENDING
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_BANK)
    currency = models.CharField(max_length=3, default="USD")
    # Color de acento en hex ("#RRGGBB"), estilo Buddy. Vacío = color por
    # defecto según el tipo/subtipo en el cliente.
    color = models.CharField(max_length=9, blank=True, default="")

    # Saldo/valor inicial (punto de partida fijo, editable).
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Saldo actual PROPIO (sin hijos) = opening_balance + Σ transacciones vivas.
    # Lo mantienen los signals; recalculable con `manage.py recompute_balances`.
    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # Si es False, la cartera no suma al patrimonio neto (sí aparece en los
    # totales por tipo).
    counts_toward_net_worth = models.BooleanField(default=True)

    # --- Ahorro ---
    goal_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    goal_date = models.DateField(null=True, blank=True)
    monthly_contribution = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    # Límite de crédito (solo tarjetas de crédito / líneas de crédito).
    credit_limit = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    # --- Tarjeta / deuda ---
    card_last4 = models.CharField(max_length=4, blank=True, null=True)
    billing_cycle_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Día del mes de fecha de corte (tarjetas)"
    )
    payment_due_day = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Día del mes de fecha límite de pago (tarjetas)"
    )
    interest_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    due_date = models.DateField(null=True, blank=True)
    counterparty = models.CharField(
        max_length=100, blank=True, help_text="Persona/entidad de la deuda"
    )

    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_SHARED
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="owned_wallets",
        help_text="Solo aplica si visibility=private",
    )

    is_active = models.BooleanField(default=True)
    # Archivada: se oculta de la lista de carteras pero sigue contando para el
    # patrimonio neto (igual que en Buddy).
    is_archived = models.BooleanField(default=False)
    # Orden manual en la lista de carteras.
    sort_order = models.PositiveIntegerField(default=0)
    # Cartera preseleccionada al crear una transacción. Máx. una por workspace.
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_default=True, is_deleted=False),
                name="one_default_wallet_per_workspace",
            ),
        ]

    def save(self, *args, **kwargs):
        # Al crear, el saldo actual arranca en el saldo de apertura.
        if self._state.adding and not self.current_balance:
            self.current_balance = self.opening_balance
        # Solo una cartera default por workspace.
        if self.is_default and self.workspace_id:
            Wallet.objects.filter(
                workspace_id=self.workspace_id, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @property
    def aggregated_balance(self):
        """Saldo propio + el de todos los descendientes. Solo para mostrar."""
        total = self.current_balance
        for child in self.children.all():
            total += child.aggregated_balance
        return total

    @property
    def progress_pct(self):
        """Avance hacia la meta de ahorro (0..1+), o None si no hay meta."""
        if not self.goal_amount:
            return None
        return float(self.current_balance) / float(self.goal_amount)

    @property
    def available_credit(self):
        """Crédito disponible de una tarjeta: límite + saldo (el saldo es
        negativo cuando debes). ``None`` si no es tarjeta o no tiene límite."""
        if self.kind != self.KIND_CREDIT or self.credit_limit is None:
            return None
        return self.credit_limit + self.current_balance

    def __str__(self):
        return f"{self.name} ({self.workspace})"
