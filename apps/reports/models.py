from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class MonthlySnapshot(BaseModel):
    """
    Fotografia del estado financiero del workspace al cierre de un mes.
    Se genera automaticamente (Celery Beat, dia 1 a las 00:05) y sirve de
    base para el historial mes a mes en la app.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="monthly_snapshots")
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    total_net_worth = models.DecimalField(max_digits=14, decimal_places=2)
    total_income = models.DecimalField(max_digits=14, decimal_places=2)
    total_expenses = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "month", "year"], name="unique_snapshot_per_month")
        ]
        ordering = ["-year", "-month"]

    def __str__(self):
        return f"{self.workspace} {self.month}/{self.year}"
