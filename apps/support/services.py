"""Reenvío de tickets nuevos a Discord vía webhook entrante -- ver
`SupportTicket` para el resto del flujo (admin de Django hace el seguimiento)."""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Discord trunca (o rechaza) mensajes larguísimos; el mensaje del ticket ya
# se puede leer completo en el admin, esto es sólo para enterarse rápido.
MAX_MESSAGE_PREVIEW = 500


def notify_new_ticket(ticket):
    """Postea el ticket recién creado al webhook de Discord configurado
    (`SUPPORT_WEBHOOK_URL`). No hace nada si no hay webhook configurado, y
    nunca revienta la creación del ticket si el POST falla -- el ticket ya
    quedó guardado y visible en el admin de todos modos."""
    webhook_url = settings.SUPPORT_WEBHOOK_URL
    if not webhook_url:
        return

    preview = ticket.message[:MAX_MESSAGE_PREVIEW]
    if len(ticket.message) > MAX_MESSAGE_PREVIEW:
        preview += "…"

    who = ticket.created_by.email if ticket.created_by else "usuario desconocido"
    meta = " · ".join(filter(None, [ticket.platform, ticket.app_version]))
    content = (
        f"**Nuevo {ticket.get_type_display().lower()}** — {ticket.subject}\n"
        f"De: {who} ({ticket.workspace.name})"
        + (f" · {meta}" if meta else "")
        + f"\n> {preview}"
    )

    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except requests.RequestException:
        logger.warning("No se pudo notificar el ticket %s a Discord.", ticket.id, exc_info=True)
