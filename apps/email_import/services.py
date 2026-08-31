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

from django.utils.text import slugify

from apps.accounts.models import Account
from apps.workspaces.models import Workspace

from .bank_parsers import ParseError, get_parser
from .models import BankEmailSchema, EmailImportLog

_TOKEN_RE = re.compile(r"\+([A-Za-z0-9_-]+)@")


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
            return Workspace.objects.get(inbound_token=match.group(1))
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

    base = dict(workspace=workspace, raw_email_subject=(subject or "")[:255])

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

    account = None
    if parsed.card_last4:
        account = Account.objects.filter(
            workspace=workspace, card_last4=parsed.card_last4
        ).first()

    return EmailImportLog.objects.create(
        status=EmailImportLog.STATUS_PENDING,
        account=account,
        extracted_amount=parsed.amount,
        extracted_merchant=(parsed.merchant or "")[:255],
        extracted_date=parsed.date,
        **base,
    )
