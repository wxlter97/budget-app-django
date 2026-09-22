"""
Chat sobre las finanzas del workspace: la IA nunca toca la base directo.

Dos llamadas a Gemini por pregunta -- no una conversación con `tools`
nativas de la API: nuestro universo de funciones es chico y fijo, y así
alcanza el mismo cliente REST minimalista de `apps.ai.client`.

1. La primera sólo ELIGE qué función de `apps.reports.services` llamar (o
   ninguna) y con qué argumentos, de una lista cerrada -- salida
   estructurada, igual que el resto de `apps.ai`. Nunca ve una fila de la
   base ni genera SQL.
2. Se ejecuta esa función ACÁ, en código, con el `workspace`/`user` reales
   -- no algo que el modelo arma. El resultado, que ya pasó por
   `visible_transactions` (carteras privadas ajenas afuera, igual que el
   resto del API), vuelve en una segunda llamada que sólo redacta la
   respuesta.

Las dos llamadas se registran como **una sola** unidad de la cuota de chat
(ver `quotas.PLAN_FEATURE_KEYS[OP_CHAT]`): se factura lo que realmente
cuesta, pero al usuario no le comen dos una pregunta que hizo una vez. Si
falla cualquiera de las dos, no se cobra nada (mismo criterio que
`services.run`).
"""
from __future__ import annotations

import datetime as dt
import json

from django.utils import timezone

from apps.common import periods
from apps.common.services import module_enabled
from apps.reports import services as reports

from . import models as m
from . import pricing, quotas
from .client import AIUnavailable, generate

MAX_QUESTION_LENGTH = 500

DECLINE_MESSAGE = (
    "Eso no lo puedo responder con los datos de tus finanzas. Contame algo sobre "
    "tus gastos, tu presupuesto o lo que tenés programado, y lo reviso."
)

# Funciones que el chat puede llamar -- cerradas a propósito (backlog, punto
# 5: "no darle acceso a la base ni generar SQL"). Cada una ya se usa en otra
# parte de la app (dashboard, reportes, recordatorios) y ya filtra por
# carteras visibles.
_FUNCTIONS = {
    "budget_vs_actual": (
        "Presupuesto vs. gasto real por categoría, del período de presupuesto "
        "que contiene la fecha dada (o el de hoy)."
    ),
    "spending_by_category": "Total gastado por categoría en un mes calendario.",
    "monthly_cashflow": "Ingresos, gastos y neto mes a mes de los últimos N meses.",
    "upcoming_scheduled": (
        "Gastos recurrentes, cuotas, pagos de tarjeta y vencimientos de deuda "
        "programados en un rango de fechas."
    ),
    "behavior_insights": (
        "Patrones de comportamiento de gasto ya detectados (fin de semana, "
        "después de cobrar, gasto hormiga, día pico, categoría o frecuencia en alza)."
    ),
}

_SELECT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "function": {
            "type": "string",
            "enum": [*_FUNCTIONS.keys(), "none"],
            "description": "Cuál función llamar, o 'none' si ninguna sirve para responder.",
        },
        "args": {
            "type": "object",
            "properties": {
                "period_start": {"type": "string", "description": "YYYY-MM-DD, para budget_vs_actual."},
                "year": {"type": "string", "description": "Para spending_by_category."},
                "month": {"type": "string", "description": "1-12, para spending_by_category."},
                "months": {"type": "string", "description": "Para monthly_cashflow (default 6)."},
                "since": {"type": "string", "description": "YYYY-MM-DD, para upcoming_scheduled."},
                "until": {"type": "string", "description": "YYYY-MM-DD, para upcoming_scheduled."},
            },
        },
        "none_reason": {
            "type": "string",
            "description": (
                "Si function es 'none': una respuesta corta y directa para la persona "
                "explicando por qué (pide consejo de inversión, no tiene que ver con sus "
                "finanzas, ninguna función cubre lo que pregunta)."
            ),
        },
    },
    "required": ["function"],
}

_SELECT_SYSTEM_INSTRUCTION = """\
Elegís qué función de datos llamar para responder una pregunta sobre las \
finanzas de un presupuesto personal (español de El Salvador y Centroamérica).

Funciones disponibles:
{functions}

Elegí UNA sola, la que mejor sirva para responder. Si la pregunta pide \
consejo de inversión, no tiene que ver con las finanzas de este workspace, o \
ninguna función cubre lo que pregunta, elegí "none" y explicá por qué en \
`none_reason`, en un tono directo y breve, como si le respondieras vos mismo.

No inventes argumentos que la pregunta no dio: dejalos vacíos y la función \
usa su propio default (el mes en curso, hoy, etc).
"""

_ANSWER_SYSTEM_INSTRUCTION = """\
Respondés una pregunta sobre las finanzas de un presupuesto personal, en \
español de El Salvador y Centroamérica, a partir de datos que ya calculó el \
backend (no los inventás, no hacés cuentas nuevas más allá de leerlos).

Reglas:
- Toda cifra que menciones cita el período (mes o rango de fechas) y la \
moneda base -- nunca un número suelto.
- Las transacciones en una moneda sin tasa de cambio configurada quedan \
afuera de los totales (así calcula el resto de la app); si por eso los \
datos vienen vacíos o incompletos, decilo en vez de asumir que no hay nada.
- No des consejos de inversión ni de productos financieros regulados: \
describí lo que pasó con los datos, no recomendés qué hacer con la plata.
- Si los datos no alcanzan para responder, decilo en vez de inventar.
- Dos o tres oraciones alcanzan casi siempre. No repitas la pregunta.

Pregunta: {question}

Datos (ya calculados, en la moneda base del workspace salvo que se indique \
lo contrario): {data}
"""


def ask(*, user, workspace, question: str) -> dict:
    """Responde una pregunta sobre las finanzas del workspace.

    Levanta `quotas.QuotaExceeded` (429) si no queda cuota, y `AIUnavailable`
    si Gemini no puede responder -- igual que el resto de `apps.ai`.
    """
    if not module_enabled("ai"):
        raise AIUnavailable("module_disabled", "La IA está desactivada.")

    question = (question or "").strip()[:MAX_QUESTION_LENGTH]
    quotas.check(user, m.OP_CHAT)

    model = pricing.model_for(m.OP_CHAT)
    calls = []
    function_used = None

    try:
        selection, select_response = _select_function(model, question)
        calls.append(select_response)

        function_name = selection.get("function")
        if function_name in _FUNCTIONS:
            data = _call_function(function_name, workspace, user, selection.get("args") or {})
            answer, answer_response = _answer(model, question, data)
            calls.append(answer_response)
            function_used = function_name
        else:
            answer = selection.get("none_reason") or DECLINE_MESSAGE
    except AIUnavailable as exc:
        _record(
            user=user, workspace=workspace, model=model, calls=calls,
            status=m.STATUS_ERROR, error_code=exc.code[:40], counts_against_quota=False,
        )
        raise

    _record(
        user=user, workspace=workspace, model=model, calls=calls,
        status=m.STATUS_OK, counts_against_quota=True,
    )
    return {"answer": answer, "function_used": function_used}


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------
def _function_list() -> str:
    return "\n".join(f"- {name}: {description}" for name, description in _FUNCTIONS.items())


def _select_function(model, question):
    response = generate(
        model=model,
        parts=[{"text": question}],
        system_instruction=_SELECT_SYSTEM_INSTRUCTION.format(functions=_function_list()),
        response_schema=_SELECT_RESPONSE_SCHEMA,
    )
    return response.json(), response


def _answer(model, question, data):
    response = generate(
        model=model,
        parts=[{"text": "Redactá la respuesta."}],
        system_instruction=_ANSWER_SYSTEM_INSTRUCTION.format(
            question=question, data=json.dumps(data, default=str, ensure_ascii=False),
        ),
    )
    return response.text.strip(), response


def _record(*, user, workspace, model, calls, status, counts_against_quota, error_code=""):
    """Una sola fila de `AIUsage` por pregunta, sume una o dos llamadas
    reales a Gemini -- ver el docstring del módulo."""
    m.AIUsage.objects.create(
        user=user,
        workspace=workspace,
        operation=m.OP_CHAT,
        model=model,
        status=status,
        error_code=error_code[:40],
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cost_micros=sum(
            pricing.estimate_cost_micros(c.model, c.input_tokens, c.output_tokens) for c in calls
        ),
        latency_ms=sum(c.latency_ms for c in calls),
        counts_against_quota=counts_against_quota,
    )


def _date_arg(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _int_arg(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _call_function(name, workspace, user, args):
    today = timezone.localdate()

    if name == "budget_vs_actual":
        period_start = _date_arg(args.get("period_start")) or periods.period_start(
            today, workspace.budget_period
        )
        return reports.budget_vs_actual(workspace, user, period_start)

    if name == "spending_by_category":
        year = _int_arg(args.get("year")) or today.year
        month = _int_arg(args.get("month")) or today.month
        return {
            "year": year, "month": month,
            "rows": reports.spending_by_category(workspace, user, year, month),
        }

    if name == "monthly_cashflow":
        months = min(max(_int_arg(args.get("months")) or 6, 1), 24)
        return reports.monthly_cashflow(workspace, user, months=months)

    if name == "upcoming_scheduled":
        return reports.upcoming_scheduled(
            workspace, user,
            since=_date_arg(args.get("since")), until=_date_arg(args.get("until")),
        )

    if name == "behavior_insights":
        # Sólo título y cuerpo: `dedupe_key` es para `NotificationLog` (trae
        # el UUID del workspace) y no le sirve de nada al modelo.
        return [
            {"title": i["title"], "body": i["body"]}
            for i in reports.behavior_insights(workspace, user, today=today)
        ]

    raise ValueError(name)  # pragma: no cover - `ask()` ya validó el nombre contra `_FUNCTIONS`
