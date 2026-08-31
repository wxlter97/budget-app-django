import secrets

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


def generate_inbound_token():
    return secrets.token_urlsafe(9)


class Workspace(BaseModel):
    """
    El "presupuesto" compartido tal como lo ve el usuario en la app.
    Se llama Workspace en el codigo (no Budget) para no chocar con
    CategoryBudget (el monto presupuestado por categoria/mes).

    Un usuario puede pertenecer a N workspaces, y un workspace puede
    tener N usuarios -- ver Membership.
    """
    name = models.CharField(max_length=100)

    # Identifica al workspace en la dirección de importación por correo
    # (import+<inbound_token>@<dominio>). Rotable si se filtra.
    inbound_token = models.CharField(
        max_length=32, unique=True, default=generate_inbound_token, editable=False
    )

    def __str__(self):
        return self.name

    @property
    def inbound_email(self) -> str:
        return (
            f"{settings.INBOUND_EMAIL_LOCALPART}+{self.inbound_token}"
            f"@{settings.INBOUND_EMAIL_DOMAIN}"
        )

    def rotate_inbound_token(self):
        self.inbound_token = generate_inbound_token()
        self.save(update_fields=["inbound_token", "updated_at"])


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
