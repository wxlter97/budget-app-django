from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class SupportTicket(BaseModel):
    """Reporte de bug, consulta o sugerencia mandado desde la app. Al crearse
    se reenvía a un canal de Discord (ver `services.notify_new_ticket`) para
    que alguien lo vea sin tener que entrar al admin a revisar. El
    seguimiento (cambiar `status`, responder) se hace desde el admin de
    Django -- no hay panel de soporte aparte todavía."""

    TYPE_BUG = "bug"
    TYPE_QUERY = "query"
    TYPE_SUGGESTION = "suggestion"
    TYPE_CHOICES = [
        (TYPE_BUG, "Reporte de error"),
        (TYPE_QUERY, "Consulta"),
        (TYPE_SUGGESTION, "Sugerencia"),
    ]

    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Abierto"),
        (STATUS_IN_PROGRESS, "En progreso"),
        (STATUS_RESOLVED, "Resuelto"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="support_tickets")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="support_tickets"
    )
    type = models.CharField(max_length=12, choices=TYPE_CHOICES, default=TYPE_BUG)
    subject = models.CharField(max_length=200)
    message = models.TextField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_OPEN)
    # Metadata automática del cliente al momento de mandar el ticket -- ayuda
    # a diagnosticar un bug sin tener que preguntarle al usuario "¿en qué
    # versión/plataforma te pasó?". Ninguno de los dos es obligatorio: un
    # cliente viejo o web podría no mandarlos.
    app_version = models.CharField(max_length=30, blank=True, default="")
    platform = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.get_type_display()}] {self.subject}"


class SupportTicketMessage(BaseModel):
    """Un mensaje del hilo de un ticket -- del usuario que lo abrió (al
    crear el ticket, o agregando más contexto después) o de soporte
    (respondido desde el admin). `is_staff_reply` es una foto fija de si
    `author` era staff en ese momento, para que el hilo no cambie de
    apariencia si el rol de esa persona cambia después."""

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="support_messages"
    )
    body = models.TextField()
    is_staff_reply = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{'Soporte' if self.is_staff_reply else 'Usuario'} en {self.ticket_id}"
