"""
SIMAN (tarjeta Credisiman) -- notificación de compra:

    Estimado(a) WALTER ERNESTO RAMIREZ CASTILLO,
    Se le informa que su tarjeta CREDISIMAN VISA GOLD 5818 ha realizado una
    compra por USD 3.99 en Almacenes Siman 0000 SV.
    En caso de que usted no haya generado esta transacción...

OJO: este correo NO trae fecha ni hora de la compra en ningún lado -- se
usa la fecha de HOY (cuando se procesa el correo) como aproximación, ya que
esta notificación llega prácticamente al instante de la compra. Si alguna
vez llega con más de un día de atraso (reintento del proveedor de correo,
etc.), la fecha de la Transaction va a quedar corrida respecto a la real;
revisar el log si eso pasa.
"""
import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .base import ParsedEmail, ParseError
from .registry import register

_LINE = re.compile(
    r"tarjeta\s+CREDISIMAN\s+.+?\s+(?P<last4>\d{4})\s+ha realizado una compra por\s+"
    r"(?P<currency>[A-Z]{3})\s+(?P<amount>[\d,]+\.\d{2})\s+en\s+(?P<merchant>.+?)\.",
    re.IGNORECASE,
)


@register("siman")
def parse(subject: str, text: str, sender: str) -> ParsedEmail:
    match = _LINE.search(text or "")
    if not match:
        raise ParseError("el cuerpo no coincide con el formato de compra de SIMAN/Credisiman")

    try:
        amount = Decimal(match["amount"].replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError(f"monto ilegible: {match['amount']!r}") from exc

    return ParsedEmail(
        amount=amount,
        date=timezone.localdate(),
        merchant=match["merchant"].strip(),
        card_last4=match["last4"],
        currency=match["currency"].upper(),
    )
