"""
BAC Credomatic (Banco de América Central) -- alerta de compra. El cuerpo es
una tabla HTML; lo de abajo es el texto plano tal como queda extraído
(encabezados de columna en su propia línea, después los valores en el mismo
orden):

    Estimado/a: WALTER ERNESTO RAMIREZ CASTILLO
    Se acaba de realizar una compra con tu tarjeta AMEX terminada en 9655
    Detalles del movimiento:

    Comercio
    Monto
    DENNYS RAMBLAS SANT
    5.99

    Fecha y hora
    2026/09/04-16:46:17

    Tipo de la compra
    Estado
    Tarjeta Presente
    Aprobada

No trae moneda explícita (el pie dice "válido únicamente para... El
Salvador") -- se asume USD, el default de `ParsedEmail`.

OJO: esto está ajustado al texto que se copió a mano del correo. Si el
correo real (el que llega por el webhook) trae otros saltos de línea o
espacios porque el proveedor de correo entrante extrae el texto distinto
de una tabla HTML, puede no matchear -- va a caer como `failed` en el log
(no se pierde, pero no se genera automático) y hay que ajustar el regex
con ese texto real.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .base import ParsedEmail, ParseError
from .registry import register

_LAST4 = re.compile(r"tarjeta\s+\w+\s+terminada en\s+(?P<last4>\d{4})", re.IGNORECASE)
_MERCHANT_AMOUNT = re.compile(
    r"Comercio\s*\n\s*Monto\s*\n\s*(?P<merchant>[^\n]+?)\s*\n\s*(?P<amount>[\d,]+\.\d{2})",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"Fecha y hora\s*\n\s*(?P<date>\d{4}/\d{2}/\d{2})-(?P<time>\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)


@register("banco-de-america-central")  # slugify("Banco de América Central")
def parse(subject: str, text: str, sender: str) -> ParsedEmail:
    text = text or ""
    last4_m = _LAST4.search(text)
    merch_m = _MERCHANT_AMOUNT.search(text)
    date_m = _DATE.search(text)
    if not (merch_m and date_m):
        raise ParseError("el cuerpo no coincide con el formato de alerta de BAC Credomatic")

    try:
        amount = Decimal(merch_m["amount"].replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError(f"monto ilegible: {merch_m['amount']!r}") from exc

    try:
        when = datetime.strptime(
            f"{date_m['date']}-{date_m['time']}", "%Y/%m/%d-%H:%M:%S"
        ).date()
    except ValueError as exc:
        raise ParseError(f"fecha ilegible: {date_m['date']!r}") from exc

    return ParsedEmail(
        amount=amount,
        date=when,
        merchant=merch_m["merchant"].strip(),
        card_last4=last4_m["last4"] if last4_m else None,
    )
