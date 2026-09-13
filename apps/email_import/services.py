"""
Ingesta de correos bancarios entrantes.

Flujo: el webhook normaliza el correo a ``(to, sender, subject, text)`` y
llama a :func:`ingest_inbound_email`, que:

1. resuelve el workspace por el token de la dirección ``import+<token>@...``;
2. busca un :class:`BankEmailSchema` activo cuyo ``sender_pattern`` (regex)
   matchee el remitente;
3. corre el parser registrado para ese banco;
4. crea un :class:`EmailImportLog` — ``pending`` si todo salió bien (queda
   esperando la confirmación manual del usuario), ``failed`` en cualquier
   otro caso, con el motivo en ``error_message``.

Nunca crea una Transaction: eso solo ocurre al confirmar el log.
"""
import re
from urllib.parse import urlparse

import requests
from django.db.models import Q
from django.utils.text import slugify

from apps.accounts.models import Wallet
from apps.workspaces.models import Workspace

from .bank_parsers import ParseError, get_parser
from .models import BankEmailSchema, EmailImportLog

_TOKEN_RE = re.compile(r"\+([A-Za-z0-9_-]+)@")

# Gmail exige "verificar" una direccion de reenvio haciendo click en un link
# que manda... a esa misma direccion (nuestro webhook). Como nadie lee esa
# casilla, sin este auto-clic ningun usuario de Gmail podria activar nunca el
# reenvio automatico nativo -- lo seguimos nosotros mismos (un GET, lo mismo
# que haria un click humano) apenas detectamos el remitente de Gmail.
_GMAIL_FORWARDING_SENDER_RE = re.compile(r"forwarding-noreply@google\.com", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _confirm_gmail_forwarding_link(text):
    """Busca en ``text`` un link de google.com y lo sigue con un GET.

    Devuelve True si encontro y siguio un link; False si no habia ninguno
    reconocible o si el request fallo (queda como `failed` para revision).
    """
    for url in _URL_RE.findall(text or ""):
        host = urlparse(url).netloc.lower()
        if host == "google.com" or host.endswith(".google.com"):
            try:
                requests.get(url, timeout=10)
            except requests.RequestException:
                return False
            return True
    return False


class WorkspaceNotResolved(Exception):
    """La dirección de destino no corresponde a ningún workspace."""


def resolve_workspace(to_addresses):
    """Devuelve el Workspace a partir del token de ``import+<token>@dominio``.

    ``to_addresses`` puede ser un string o una lista (un correo reenviado
    suele tener varias direcciones en el ``To``).
    """
    if isinstance(to_addresses, str):
        to_addresses = [to_addresses]

    for address in to_addresses:
        match = _TOKEN_RE.search(address or "")
        if not match:
            continue
        try:
            # `iexact`, no `=`: casi todos los proveedores de correo entrante
            # (Mailgun incluido) normalizan el destinatario a minúsculas antes
            # de mandarlo, y el token guardado puede tener mayúsculas (viene
            # de `secrets.token_urlsafe`) — una comparación exacta pierde el
            # workspace real y esto se ve como un 404 "misterioso".
            return Workspace.objects.get(inbound_token__iexact=match.group(1))
        except Workspace.DoesNotExist:
            continue
    raise WorkspaceNotResolved(f"Sin workspace para {to_addresses!r}")


def _match_schema(sender):
    for schema in BankEmailSchema.objects.filter(is_active=True):
        try:
            if re.search(schema.sender_pattern, sender or "", re.IGNORECASE):
                return schema
        except re.error:
            # patrón mal escrito: tratarlo como substring literal
            if schema.sender_pattern.lower() in (sender or "").lower():
                return schema
    return None


def ingest_inbound_email(*, to, sender, subject="", text="", workspace=None):
    if workspace is None:
        workspace = resolve_workspace(to)

    base = dict(
        workspace=workspace,
        raw_email_subject=(subject or "")[:255],
        raw_email_body=text or "",
    )

    if _GMAIL_FORWARDING_SENDER_RE.search(sender or ""):
        if _confirm_gmail_forwarding_link(text):
            return EmailImportLog.objects.create(status=EmailImportLog.STATUS_AUTO_HANDLED, **base)
        return EmailImportLog.objects.create(
            status=EmailImportLog.STATUS_FAILED,
            error_message="Correo de confirmación de reenvío de Gmail sin link reconocible.",
            **base,
        )

    schema = _match_schema(sender)
    if schema is None:
        return EmailImportLog.objects.create(
            status=EmailImportLog.STATUS_FAILED,
            error_message=f"Remitente no reconocido: {sender}"[:500],
            **base,
        )

    base["bank_schema"] = schema
    parser = get_parser(slugify(schema.bank_name))
    if parser is None:
        return EmailImportLog.objects.create(
            status=EmailImportLog.STATUS_FAILED,
            error_message=f"Sin parser para '{schema.bank_name}'."[:500],
            **base,
        )

    try:
        parsed = parser(subject, text, sender)
    except ParseError as exc:
        return EmailImportLog.objects.create(
            status=EmailImportLog.STATUS_FAILED,
            error_message=str(exc)[:500],
            **base,
        )

    wallet = None
    if parsed.card_last4:
        # Además del `card_last4` principal, una cartera puede tener
        # plásticos adicionales (titular + adicionales de la misma cuenta,
        # ver `Wallet.extra_cards`) -- cualquiera de los dos hace caer el
        # correo en la misma cartera.
        wallet = Wallet.objects.filter(
            Q(card_last4=parsed.card_last4) | Q(extra_cards__last4=parsed.card_last4),
            workspace=workspace,
        ).first()
    if wallet is None:
        # Fallback por banco: si el correo no trae los últimos 4 dígitos (o
        # no matchean ninguna cartera) y el workspace tiene EXACTAMENTE una
        # cartera activa marcada con este banco, asumimos que es esa -- con
        # dos o más queda ambiguo y se deja sin asignar (el usuario la elige
        # a mano al confirmar).
        candidates = list(
            Wallet.objects.filter(workspace=workspace, bank_schema=schema, is_active=True)
        )
        if len(candidates) == 1:
            wallet = candidates[0]

    log = EmailImportLog.objects.create(
        status=EmailImportLog.STATUS_PENDING,
        wallet=wallet,
        extracted_amount=parsed.amount,
        extracted_merchant=(parsed.merchant or "")[:255],
        extracted_date=parsed.date,
        **base,
    )
    _notify_pending_email_import(log)
    return log


def _notify_pending_email_import(log):
    """A cualquier miembro del workspace (no hay un dueño único del
    `EmailImportLog`) -- ver `apps.notifications.services.notify_user`."""
    # Import diferido: evita el ciclo apps.email_import <-> apps.notifications.
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_user
    from apps.workspaces.models import Membership

    memberships = Membership.objects.filter(
        workspace=log.workspace, is_deleted=False
    ).select_related("user")
    for membership in memberships:
        notify_user(
            membership.user,
            kind=Notification.KIND_EMAIL_IMPORT_PENDING,
            title="Correo bancario por revisar",
            body=f"{log.extracted_merchant or log.raw_email_subject or 'Nuevo movimiento'} "
            f"— {log.workspace.name}",
            workspace=log.workspace,
            data={
                "type": Notification.KIND_EMAIL_IMPORT_PENDING,
                "workspace": str(log.workspace.id),
                "log_id": str(log.id),
            },
            related_object_id=log.id,
        )
