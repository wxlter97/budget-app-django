"""
Notificación de invitaciones pendientes de ANTES de tener cuenta.

`apps.workspaces.services.get_or_create_invitation` sólo crea una
`Invitation` cuando el correo invitado todavía no tenía cuenta (si ya la
tenía, se agrega directo como Membership, sin invitación -- ver
`MembershipViewSet.create`). Eso significa que recién hay a QUIÉN
notificarle en el centro de notificaciones una vez que esa cuenta se crea
(registro, o "Continuar con Google" por primera vez) -- de ahí que esto
viva en un `post_save` de `User` en vez de en el momento de invitar.
"""
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Notification
from .services import notify_user


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def notify_pending_invitations_on_user_created(sender, instance, created, **kwargs):
    if not created or not instance.email:
        return

    # Import diferido: evita el ciclo apps.notifications <-> apps.workspaces
    # (mismo patrón que `apps.common.api.resolve_workspace`).
    from apps.workspaces.models import Invitation

    pending = Invitation.objects.filter(
        email__iexact=instance.email, status=Invitation.STATUS_PENDING
    ).select_related("workspace")
    for invitation in pending:
        notify_user(
            instance,
            kind=Notification.KIND_INVITATION,
            title="Te invitaron a un presupuesto",
            body=f'Te invitaron a "{invitation.workspace.name}"',
            workspace=invitation.workspace,
            data={
                "type": Notification.KIND_INVITATION,
                "workspace": str(invitation.workspace.id),
                "invitation_id": str(invitation.id),
            },
            related_object_id=invitation.id,
        )


def _resolve_card_payment(sender, instance, created, **kwargs):
    """Un abono a una tarjeta con corte (transferencia entrante o ingreso)
    puede dejar pagado su estado de cuenta: resuelve esos avisos en el
    centro de notificaciones (ver `resolve_paid_statement_notifications`)."""
    from apps.accounts.models import Wallet
    from apps.transactions.models import Transaction

    from .services import resolve_paid_statement_notifications

    if instance.is_deleted:
        return
    if instance.type == Transaction.TYPE_TRANSFER:
        target_id = instance.to_wallet_id
    elif instance.type == Transaction.TYPE_INCOME:
        target_id = instance.wallet_id
    else:
        return
    wallet = Wallet.objects.filter(
        id=target_id, kind=Wallet.KIND_CREDIT, billing_cycle_day__isnull=False
    ).first()
    if wallet is not None:
        resolve_paid_statement_notifications(wallet)


post_save.connect(
    _resolve_card_payment,
    sender="transactions.Transaction",
    dispatch_uid="notifications_resolve_card_payment",
)
