"""
Parser de ejemplo / plantilla.

Formato que reconoce (una línea del cuerpo del correo)::

    Compra por USD 1,234.56 en STARBUCKS con tarjeta terminada en 4321 el 20/01/2026

Sirve como referencia para escribir parsers de bancos reales.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .base import ParsedEmail, ParseError
from .registry import register

_LINE = re.compile(
    r"Compra por\s+(?P<currency>[A-Z]{3})\s+(?P<amount>[\d.,]+)\s+"
    r"en\s+(?P<merchant>.+?)\s+"
    r"con tarjeta terminada en\s+(?P<last4>\d{4})\s+"
    r"el\s+(?P<date>\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)


def _to_decimal(raw: str) -> Decimal:
    # "1,234.56" -> "1234.56"
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError(f"monto ilegible: {raw!r}") from exc


@register("demo-bank")
def parse(subject: str, text: str, sender: str) -> ParsedEmail:
    match = _LINE.search(text or "")
    if not match:
        raise ParseError("el cuerpo no coincide con el formato de Demo Bank")

    try:
        when = datetime.strptime(match["date"], "%d/%m/%Y").date()
    except ValueError as exc:
        raise ParseError(f"fecha ilegible: {match['date']!r}") from exc

    return ParsedEmail(
        amount=_to_decimal(match["amount"]),
        date=when,
        merchant=match["merchant"].strip(),
        card_last4=match["last4"],
        currency=match["currency"].upper(),
    )
