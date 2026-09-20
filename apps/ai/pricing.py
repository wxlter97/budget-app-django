"""
Qué modelo atiende cada operación y cuánto cuesta.

Vive en código y no en base ni en variables de entorno a propósito: no es un
precio que cobramos, es la **estimación** con la que se llena `AIUsage.
cost_micros` para poder mirar el gasto por operación sin esperar la factura de
Google. La factura real siempre manda; esto sirve para saber de dónde salió.

> Precios de la API de Gemini al **20 sep 2026** (tier de pago), en dólares por
> millón de tokens. **Los de `gemini-3.8-flash` son promocionales hasta el
> 31-dic-2026** ($0.75 / $3.75) y después suben ($1.50 / $7.50), y por eso la
> asignación de modelos está acá arriba y no desparramada: cuando cambien, se
> cambia en un solo lugar y el registro de consumo muestra el antes y el después.
>
> **Los modelos 2.5 ya no están disponibles para cuentas nuevas**: la API
> responde 404 ("no longer available to new users") aunque figuren en la lista
> de modelos y en la página de deprecaciones. Se dejan en la tabla sólo para
> poder costear filas viejas de `AIUsage`; no se usan.
"""
from apps.ai import models as m

# Dólares por millón de tokens: (entrada, salida).
PRICES_PER_MTOK = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),  # promocional hasta el 31-dic-2026
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-pro": (2.00, 12.00),
}

# Qué modelo atiende cada operación.
#
# El criterio es el del backlog: Flash-Lite para lo que es extracción de
# estructura a partir de texto corto (el modelo grande no acierta más y cuesta
# 6 veces), y Flash para lo que necesita visión o razonar sobre varios datos.
# El chat es más de la mitad del costo de un usuario intensivo, así que va en
# Flash-Lite: la diferencia se nota en la factura, no en las respuestas.
MODEL_FOR_OPERATION = {
    m.OP_RECEIPT: "gemini-3.8-flash",       # necesita visión
    m.OP_PARSE: "gemini-3.5-flash-lite",    # texto corto → JSON
    m.OP_CHAT: "gemini-3.5-flash-lite",
    m.OP_SUMMARY: "gemini-3.8-flash",       # razona sobre el mes entero
}

# El dictado manda audio. En la serie 3 Gemini publica el mismo precio de
# entrada para audio que para texto (verificar en la página de precios si algún
# día vuelve a cobrarse aparte, como pasaba con la 2.5).
MODEL_FOR_AUDIO = "gemini-3.8-flash"
AUDIO_INPUT_PRICE_PER_MTOK = 0.75


def model_for(operation: str, *, has_audio: bool = False) -> str:
    if has_audio:
        return MODEL_FOR_AUDIO
    return MODEL_FOR_OPERATION[operation]


def estimate_cost_micros(model: str, input_tokens: int, output_tokens: int, *, has_audio=False) -> int:
    """Costo estimado en millonésimas de dólar (ver `AIUsage.cost_micros`).

    Un modelo que no esté en la tabla devuelve 0 en vez de reventar: que el
    catálogo de precios esté desactualizado no es razón para tirar abajo una
    respuesta que el usuario ya pagó.
    """
    if model not in PRICES_PER_MTOK:
        return 0
    price_in, price_out = PRICES_PER_MTOK[model]
    if has_audio:
        price_in = AUDIO_INPUT_PRICE_PER_MTOK
    usd = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return round(usd * 1_000_000)
