"""Gamificación (subset inicial): racha de días sin gasto fuera de
presupuesto y badges por hitos. El catálogo de badges es fijo (ver
`services.BADGE_CATALOG`, sembrado por una migración de datos); lo único
que crece por workspace es `WorkspaceBadge` (qué badges ya ganó).

No hay modelo de "no-spend day" ni de streak: ambos se calculan al vuelo
desde `Transaction` (ver `services.py`) para no duplicar la fuente de
verdad -- un backfill/corrección de transacciones vieja no dejaría
desincronizada una racha guardada aparte."""
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class Badge(BaseModel):
    """Catálogo fijo de logros posibles. No es por workspace: es el mismo
    para todos, sembrado por una migración de datos."""

    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255)
    icon = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class WorkspaceBadge(BaseModel):
    """Un badge que un workspace ya ganó. Se otorga de forma perezosa (ver
    `services.evaluate_and_award_badges`, llamada en cada consulta del
    resumen) -- no hay tarea periódica dedicada."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="earned_badges")
    badge = models.ForeignKey(Badge, on_delete=models.PROTECT, related_name="awards")
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["earned_at"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "badge"], name="unique_badge_per_workspace"),
        ]

    def __str__(self):
        return f"{self.workspace_id} -> {self.badge.code}"
