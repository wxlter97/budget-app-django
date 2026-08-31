from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class Workspace(BaseModel):
    """
    El "presupuesto" compartido tal como lo ve el usuario en la app.
    Se llama Workspace en el codigo (no Budget) para no chocar con
    CategoryBudget (el monto presupuestado por categoria/mes).

    Un usuario puede pertenecer a N workspaces, y un workspace puede
    tener N usuarios -- ver Membership.
    """
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Membership(BaseModel):
    ROLE_OWNER = "owner"
    ROLE_MEMBER = "member"
    ROLE_CHOICES = [(ROLE_OWNER, "Owner"), (ROLE_MEMBER, "Member")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="unique_membership_per_workspace")
        ]

    def __str__(self):
        return f"{self.user} in {self.workspace} ({self.role})"
