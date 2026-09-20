"""
El único camino por el que el resto del proyecto usa IA.

Recibos, parseo de texto, voz, chat y resumen mensual van a llamar a `run()`:
así el chequeo de cuota, el registro de consumo y el manejo del error quedan
escritos una sola vez y no cinco, con cuatro variantes sutilmente distintas.

Quien llama se ocupa de armar el prompt y de interpretar la respuesta, que es
lo propio de cada función. Todo lo demás es de acá.
"""
from __future__ import annotations

import logging

from apps.common.services import module_enabled

from . import models as m
from . import pricing, quotas
from .client import AIUnavailable, GeminiResponse, generate, is_enabled

logger = logging.getLogger(__name__)


def run(
    *,
    user,
    operation: str,
    parts: list[dict],
    system_instruction: str = "",
    response_schema: dict | None = None,
    workspace=None,
    has_audio: bool = False,
) -> GeminiResponse:
    """Chequea la cuota, llama a Gemini y registra el consumo.

    Levanta `quotas.QuotaExceeded` (429) si al usuario no le quedan, y
    `AIUnavailable` si Gemini no pudo responder. Las dos son esperables: la
    primera es una decisión de producto y la segunda es un servicio de
    terceros, y ninguna debería terminar en un 500.

    **Sobre la carrera:** dos requests en paralelo del mismo usuario pueden
    pasar el chequeo a la vez y gastar una llamada de más. Está asumido: el
    daño máximo es una unidad por ráfaga, el throttle de DRF (`ai`, 12/min)
    acota la ráfaga, y la alternativa — bloquear filas o reservar cupo — es
    mucha maquinaria para proteger medio centavo.
    """
    # El interruptor manual (`ModuleFlag` "ai") corta el gasto acá, en el
    # servidor. `availability_for` sólo alimenta al front para esconder los
    # botones, pero /ai/receipt/ y /ai/parse/ se pueden llamar directo con un
    # token válido, y todas las funciones pasan por este único camino. Va antes
    # de la cuota: no consulta nada ni deja fila en `AIUsage`.
    if not module_enabled("ai"):
        raise AIUnavailable("module_disabled", "La IA está desactivada.")

    quotas.check(user, operation)

    model = pricing.model_for(operation, has_audio=has_audio)
    try:
        response = generate(
            model=model,
            parts=parts,
            system_instruction=system_instruction,
            response_schema=response_schema,
        )
    except AIUnavailable as exc:
        # Se registra igual, con `counts_against_quota=False`: sirve para ver
        # cuánto está fallando Gemini, pero el usuario no paga con su cuota un
        # error que no es suyo.
        m.AIUsage.objects.create(
            user=user,
            workspace=workspace,
            operation=operation,
            model=model,
            status=m.STATUS_ERROR,
            error_code=exc.code[:40],
            counts_against_quota=False,
        )
        raise

    m.AIUsage.objects.create(
        user=user,
        workspace=workspace,
        operation=operation,
        model=response.model,
        status=m.STATUS_OK,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_micros=pricing.estimate_cost_micros(
            response.model, response.input_tokens, response.output_tokens, has_audio=has_audio
        ),
        latency_ms=response.latency_ms,
        # El resumen mensual lo dispara el servidor, no el usuario: se registra
        # para que se vea en el gasto, pero no le come la cuota a nadie.
        counts_against_quota=operation in quotas.PLAN_FEATURE_KEYS,
    )
    return response


def availability_for(user) -> dict:
    """Lo que el front pregunta una vez para saber si mostrar las entradas de
    IA y cuánto le queda al usuario.

    `enabled` combina dos apagadores distintos: sin `GEMINI_API_KEY` (esta
    instalación nunca tuvo IA) y el interruptor manual `ModuleFlag` "ai" (la
    tenía, pero un admin la apagó porque empezó a fallar) -- el cliente no
    necesita distinguirlos, los dos significan "no mostrar las entradas de
    IA todavía".
    """
    return {"enabled": is_enabled() and module_enabled("ai"), **quotas.status_for(user)}
