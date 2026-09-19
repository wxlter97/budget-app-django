"""
Cómo se le cree (y cómo no) a lo que devuelve el modelo.

Esto vive aparte porque es lo que comparten todas las funciones de IA que
terminan en una transacción — leer un recibo, parsear una frase, dictar por
voz — y porque es donde está el riesgo de verdad. Llamar a Gemini es la parte
fácil; la difícil es que un número alucinado o una fecha del año que viene no
terminen en el saldo de alguien.

La regla que ordena todo el archivo: **vale más un campo vacío que uno
inventado.** Al vacío el usuario lo llena; al inventado no lo mira.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from django.utils import timezone

# Niveles que puede devolver el modelo. La app usa `low` para marcar el campo
# y pedirle al usuario que lo mire, no para esconderlo.
CONFIDENCE_LEVELS = ["high", "medium", "low"]

# Un monto por encima de esto, en una app de gastos personales, es una lectura
# mala mucho más seguido de lo que es un dato.
_MAX_AMOUNT = Decimal("1000000")

# Un "recibo" de hace más de dos años casi siempre es un año mal leído (2019
# por 2029, típico en tickets térmicos gastados) o una frase mal interpretada.
_MAX_AGE_DAYS = 730


def amount(value):
    """Monto a `Decimal`, o `None` si lo que vino no es un monto usable.

    El texto puede traer símbolo de moneda, separador de miles o estar vacío.
    Los montos se le piden al modelo como **texto** justamente para llegar
    acá: un JSON con `12.50` vuelve como float, y de ahí a Decimal hay un
    redondeo que no tiene por qué existir cuando se trata de plata.
    """
    if value in (None, ""):
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None
    if parsed <= 0 or parsed >= _MAX_AMOUNT:
        return None
    return parsed.quantize(Decimal("0.01"))


def date(value, *, today=None):
    """Fecha en `AAAA-MM-DD`, con hoy como red de contención.

    Una fecha que no se puede leer, futura, o demasiado vieja cae a hoy — que
    es lo que el usuario habría puesto a mano — y quien llama se encarga de
    marcar ese campo como poco confiable.
    """
    today = today or timezone.localdate()
    try:
        parsed = dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return today
    if parsed > today or parsed < today - dt.timedelta(days=_MAX_AGE_DAYS):
        return today
    return parsed


def currency(value):
    """Código ISO de 3 letras, o `None`.

    Es informativa: la moneda real de la transacción sale de la cartera
    (`Transaction.currency` se denormaliza de `wallet.currency` en cada save).
    Sirve para avisarle al usuario que esto parece de otra moneda.
    """
    code = (value or "").strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


def text(value, *, max_length=255):
    return (value or "").strip()[:max_length]


def confidence(raw, *, fields, unusable=()):
    """Lo que dijo el modelo, corregido por lo que efectivamente se pudo usar.

    El modelo puede decir "high" de un monto y devolver algo que no parsea.
    Para la app lo que importa es si el campo llegó a tener valor, así que eso
    manda sobre la opinión del modelo: los nombres que lleguen en `unusable`
    quedan en `low` pase lo que pase.
    """
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for field in fields:
        level = raw.get(field)
        result[field] = level if level in CONFIDENCE_LEVELS else "low"
    for field in unusable:
        if field in result:
            result[field] = "low"
    return result
