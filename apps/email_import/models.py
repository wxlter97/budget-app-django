from django.db import models

from apps.accounts.models import Account
from apps.common.models import BaseModel
from apps.transactions.models import Transaction
from apps.workspaces.models import Workspace


class BankEmailSchema(BaseModel):
    """
    Definicion de como reconocer y parsear las notificaciones de un banco
    especifico. Agregar soporte para un banco nuevo = crear un registro
    (+ su parser en bank_parsers/<bank>.py) sin tocar el resto del sistema.
    """
    bank_name = models.CharField(max_length=100, unique=True)
    sender_pattern = models.CharField(
        max_length=255, help_text="Dominio o patron de remitente que identifica a este banco"
    )
    parser_version = models.CharField(max_length=20, default="v1")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.bank_name} ({self.parser_version})"


class EmailImportLog(BaseModel):
    """
    Registro de cada correo procesado -- exitoso o fallido. Si un banco
    cambia su formato y el parser deja de reconocerlo, el correo queda
    aqui para revision manual en vez de perderse silenciosamente.
    """
    STATUS_PENDING = "pending"       # candidata generada, esperando confirmacion del usuario
    STATUS_CONFIRMED = "confirmed"   # el usuario aprobo y se creo la Transaction
    STATUS_REJECTED = "rejected"     # el usuario descarto la candidata
    STATUS_FAILED = "failed"         # no se pudo parsear (banco sin schema o formato no reconocido)
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente de confirmar"),
        (STATUS_CONFIRMED, "Confirmada"),
        (STATUS_REJECTED, "Rechazada"),
        (STATUS_FAILED, "Fallo de parseo"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="email_import_logs")
    bank_schema = models.ForeignKey(
        BankEmailSchema, on_delete=models.SET_NULL, null=True, related_name="import_logs"
    )
    account = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_import_logs"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    raw_email_subject = models.CharField(max_length=255, blank=True)
    extracted_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    extracted_merchant = models.CharField(max_length=255, blank=True)
    extracted_date = models.DateField(null=True, blank=True)
    resulting_transaction = models.OneToOneField(
        Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="import_log"
    )
    error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.bank_schema}: {self.status}"
