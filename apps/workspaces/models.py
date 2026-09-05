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

    # Moneda en la que se expresan los totales agregados (patrimonio neto,
    # presupuesto, flujo de caja) cuando el workspace tiene carteras en más
    # de una moneda -- ver ExchangeRate y apps.workspaces.currency.
    base_currency = models.CharField(max_length=3, default="USD")

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


def generate_invite_token():
    return secrets.token_urlsafe(24)


class Invitation(BaseModel):
    """
    Invitación a un workspace por correo, para alguien que TODAVÍA NO tiene
    cuenta (si ya la tiene, `MembershipViewSet.create` lo agrega directo sin
    pasar por acá). Se manda un correo con un enlace que trae `token`; al
    abrirlo -- ya logueado con una cuenta de ese mismo correo, registrándose
    si hacía falta -- se acepta con `accept` y recién ahí se crea la
    Membership.
    """

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_ACCEPTED, "Aceptada"),
        (STATUS_DECLINED, "Rechazada"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=10, choices=Membership.ROLE_CHOICES, default=Membership.ROLE_MEMBER)
    token = models.CharField(max_length=64, unique=True, default=generate_invite_token, editable=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # No dos invitaciones pendientes al mismo correo en el mismo
            # workspace -- reinvitar reutiliza la existente (ver services).
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(status="pending", is_deleted=False),
                name="one_pending_invitation_per_email_per_workspace",
            ),
        ]

    def __str__(self):
        return f"{self.email} -> {self.workspace} ({self.status})"


class ExchangeRate(BaseModel):
    """
    Tasa manual (sin API externa, fuera de alcance): ``1 <currency> =
    <rate_to_base> <workspace.base_currency>``. Sin tasa configurada para
    una moneda, las carteras/transacciones en esa moneda simplemente no
    entran en los totales agregados -- ver ``apps.workspaces.currency``.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="exchange_rates")
    currency = models.CharField(max_length=3)
    rate_to_base = models.DecimalField(max_digits=18, decimal_places=6)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "currency"], name="unique_rate_per_currency")
        ]
        ordering = ["currency"]

    def __str__(self):
        return f"1 {self.currency} = {self.rate_to_base} {self.workspace.base_currency}"
