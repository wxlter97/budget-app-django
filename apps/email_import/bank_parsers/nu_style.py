"""
Segundo parser de ejemplo, con un layout distinto (multilínea, monto con
símbolo, fecha en texto), para mostrar que cada banco es independiente.

    Realizaste una compra
    Valor: $ 89.900,00
    Comercio: RAPPI COLOMBIA
    Tarjeta: ****1234
    Fecha: 3 feb 2026
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .base import ParsedEmail, ParseError
from .registry import register

_MESES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _field(text, label):
    m = re.search(rf"{label}\s*:\s*(.+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


@register("nu-style")
def parse(subject: str, text: str, sender: str) -> ParsedEmail:
    text = text or ""
    raw_value = _field(text, "Valor")
    raw_merchant = _field(text, "Comercio")
    raw_card = _field(text, "Tarjeta")
    raw_date = _field(text, "Fecha")
    if not (raw_value and raw_date):
        raise ParseError("faltan campos Valor/Fecha")

    # "$ 89.900,00" -> "89900.00"  (formato es-CO: . miles, , decimales)
    digits = re.sub(r"[^\d.,]", "", raw_value).replace(".", "").replace(",", ".")
    try:
        amount = Decimal(digits)
    except InvalidOperation as exc:
        raise ParseError(f"monto ilegible: {raw_value!r}") from exc

    try:
        day, mon, year = raw_date.lower().split()
        when = datetime(int(year), _MESES[mon[:3]], int(day)).date()
    except (ValueError, KeyError) as exc:
        raise ParseError(f"fecha ilegible: {raw_date!r}") from exc

    last4 = None
    if raw_card:
        m = re.search(r"(\d{4})\s*$", raw_card)
        last4 = m.group(1) if m else None

    return ParsedEmail(
        amount=amount,
        date=when,
        merchant=(raw_merchant or "").strip(),
        card_last4=last4,
        currency="COP",
    )
