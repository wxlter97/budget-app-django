from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class SavingsGoal(BaseModel):
    """Objetivo de ahorro con meta final (ej. viaje, enganche de casa)."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="savings_goals")
    name = models.CharField(max_length=100)
    target_amount = models.DecimalField(max_digits=14, decimal_places=2)
    current_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_date = models.DateField(null=True, blank=True)
    monthly_contribution_suggested = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    @property
    def progress_pct(self):
        if self.target_amount == 0:
            return 0
        return float(self.current_amount) / float(self.target_amount)

    def __str__(self):
        return self.name


class ReserveFund(BaseModel):
    """
    Colchon recurrente para gastos futuros no mensuales (seguro anual,
    mantenimiento). A diferencia de SavingsGoal, no tiene una meta final
    fija -- se repone despues de cada uso.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="reserve_funds")
    name = models.CharField(max_length=100)
    current_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_contribution = models.DecimalField(max_digits=14, decimal_places=2)

    def __str__(self):
        return self.name
