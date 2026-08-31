from dataclasses import dataclass
from datetime import date
from decimal import Decimal


class ParseError(Exception):
    """El texto del correo no coincide con el formato esperado del banco."""


@dataclass
class ParsedEmail:
    """Resultado de parsear una notificación bancaria."""

    amount: Decimal
    date: date
    merchant: str = ""
    card_last4: str | None = None
    currency: str = "USD"
