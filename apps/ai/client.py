"""
Envoltorio fino sobre la API REST de Gemini. Es el único lugar del proyecto
que conoce `GEMINI_API_KEY` y el único que sale a internet a hablar con Google.

Se usa la API REST directamente y no el SDK de Google a propósito: lo que
necesitamos es un POST con JSON y una respuesta con JSON, y el SDK trae su
propia cadena de dependencias y su propio ciclo de releases para eso. Si algún
día hacen falta streaming o herramientas del lado de Google, se reevalúa.

**Regla que ordena todo este archivo: que la IA se caiga nunca debe tumbar
nada.** Cualquier fallo — sin key, timeout, 500 de Google, JSON que no parsea —
sale como `AIUnavailable`, y quien llama decide. El alta manual de una
transacción tiene que seguir funcionando con Gemini caído, porque es el camino
que siempre estuvo ahí.
"""
from __future__ import annotations

import json
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Un reintento y nada más. Estas llamadas son interactivas: alguien está
# mirando una pantalla de carga, y un segundo intento ya empuja el peor caso a
# ~50 s. Se reintenta sólo lo que tiene sentido reintentar (un corte de red, un
# 429/5xx de Google), nunca un 400, que va a fallar igual.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_BACKOFF_SECONDS = 1.0


class AIUnavailable(Exception):
    """La IA no pudo responder. `code` es corto y sin datos del usuario — es
    lo que termina en `AIUsage.error_code`."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def is_enabled() -> bool:
    """Sin key, todas las funciones de IA quedan apagadas y el front no
    muestra sus entradas (mismo patrón que VAPID y el botón de Google)."""
    return bool(settings.GEMINI_API_KEY)


class GeminiResponse:
    """Lo que devuelve una llamada: el texto, y lo que hace falta para
    registrar el consumo."""

    def __init__(self, text: str, model: str, input_tokens: int, output_tokens: int, latency_ms: int):
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms

    def json(self) -> dict:
        """El texto parseado como JSON.

        Aunque se pida `response_mime_type=application/json`, un modelo puede
        devolver algo que no parsea. Eso es `AIUnavailable` y no un 500: para
        quien llama es lo mismo que si Gemini no hubiera contestado.
        """
        try:
            return json.loads(self.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AIUnavailable("invalid_json", "La IA respondió algo que no es JSON.") from exc


def generate(
    *,
    model: str,
    parts: list[dict],
    system_instruction: str = "",
    response_schema: dict | None = None,
    temperature: float = 0.0,
) -> GeminiResponse:
    """Una llamada a `:generateContent`.

    `parts` es la lista de partes de Gemini: `{"text": "..."}` para texto y
    `{"inline_data": {"mime_type": ..., "data": <base64>}}` para una imagen, un
    PDF o un audio. Construirlas es tarea de quien llama, que es el que sabe
    qué le está mandando.

    `response_schema` activa la salida estructurada de Gemini: el modelo se
    ata al esquema en vez de que nosotros adivinemos con una expresión regular
    sobre prosa. Es lo que hace que `GeminiResponse.json()` sea confiable.

    `temperature=0` porque todo lo que hacemos acá es extracción: de un mismo
    recibo queremos el mismo monto siempre, no variedad.
    """
    if not is_enabled():
        raise AIUnavailable("no_api_key", "No hay GEMINI_API_KEY configurada.")

    payload: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature},
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if response_schema is not None:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = response_schema

    url = f"{settings.GEMINI_API_BASE}/models/{model}:generateContent"
    # La key va por header y no en la query string: las URLs quedan en los
    # logs de acceso de cualquier proxy en el camino.
    headers = {"x-goog-api-key": settings.GEMINI_API_KEY, "Content-Type": "application/json"}

    started = time.monotonic()
    data = _post_with_one_retry(url, payload, headers)
    latency_ms = int((time.monotonic() - started) * 1000)

    return GeminiResponse(
        text=_first_text(data),
        model=model,
        input_tokens=_usage(data, "promptTokenCount"),
        output_tokens=_usage(data, "candidatesTokenCount"),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------
def _post_with_one_retry(url, payload, headers):
    last: AIUnavailable | None = None
    for attempt in (1, 2):
        try:
            return _post(url, payload, headers)
        except AIUnavailable as exc:
            last = exc
            retryable = exc.code == "timeout" or exc.code == "network" or (
                exc.code.startswith("http_") and int(exc.code[5:]) in _RETRY_STATUSES
            )
            if not retryable or attempt == 2:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS)
    raise last  # pragma: no cover - inalcanzable, el for siempre sale antes


def _post(url, payload, headers):
    try:
        response = requests.post(
            url, json=payload, headers=headers, timeout=settings.AI_TIMEOUT_SECONDS
        )
    except requests.Timeout as exc:
        raise AIUnavailable("timeout", "Gemini no respondió a tiempo.") from exc
    except requests.RequestException as exc:
        raise AIUnavailable("network", "No se pudo llegar a Gemini.") from exc

    if response.status_code != 200:
        # El cuerpo del error puede traer el prompt de vuelta, así que va al
        # log del servidor y nunca al usuario ni a `AIUsage.error_code`.
        logger.warning("Gemini devolvió %s: %s", response.status_code, response.text[:500])
        raise AIUnavailable(f"http_{response.status_code}", "Gemini devolvió un error.")

    try:
        return response.json()
    except ValueError as exc:
        raise AIUnavailable("bad_response", "Gemini devolvió algo que no es JSON.") from exc


def _first_text(data: dict) -> str:
    """El texto del primer candidato.

    Si no hay candidatos suele ser un bloqueo por filtros de seguridad —
    improbable con recibos y montos, pero posible con una nota libre — y se
    trata como cualquier otra indisponibilidad.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "")
        raise AIUnavailable(f"no_candidates_{reason}".rstrip("_"), "Gemini no devolvió respuesta.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts)


def _usage(data: dict, key: str) -> int:
    return int((data.get("usageMetadata") or {}).get(key, 0) or 0)
