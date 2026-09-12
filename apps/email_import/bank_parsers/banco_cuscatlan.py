"""
Banco Cuscatlán -- notificación de compra (tarjeta titular o adicional, es
la misma oración en ambos casos).

    Estimado Cliente: WALTER

    Se ha realizado una compra con su tarjeta adicional de Banco CUSCATLAN
    XXXXXXXXXX9126 por USD 13.00 en UNO SAN FERNANDO AHUA el día
    2026-09-11 16:35. Consultas al 22122000.

Sólo cubre "Compra con Tarjeta de Crédito" (titular/adicional) -- si el
banco manda otros tipos (retiro, pago, transferencia) con otra redacción,
agregar otro regex acá y probar cuál matchea, o levantar `ParseError` para
que quede en el log para revisión manual en vez de perderse.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .base import ParsedEmail, ParseError
from .registry import register

_LINE = re.compile(
    r"tarjeta (?:titular|adicional) de Banco CUSCATLAN\s+X+(?P<last4>\d{4})\s+"
    r"por\s+(?P<currency>[A-Z]{3})\s+(?P<amount>[\d,]+\.\d{2})\s+"
    r"en\s+(?P<merchant>.+?)\s+el día\s+(?P<date>\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}",
    re.IGNORECASE,
)


@register("banco-cuscatlan")  # slugify("Banco Cuscatlán")
def parse(subject: str, text: str, sender: str) -> ParsedEmail:
    match = _LINE.search(text or "")
    if not match:
        raise ParseError("el cuerpo no coincide con el formato de compra de Banco Cuscatlán")

    try:
        amount = Decimal(match["amount"].replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError(f"monto ilegible: {match['amount']!r}") from exc

    try:
        when = datetime.strptime(match["date"], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ParseError(f"fecha ilegible: {match['date']!r}") from exc

    return ParsedEmail(
        amount=amount,
        date=when,
        merchant=match["merchant"].strip(),
        card_last4=match["last4"],
        currency=match["currency"].upper(),
    )
