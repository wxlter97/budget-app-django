"""
Redactar el resumen mensual a partir de los patrones ya detectados.

`apps.reports.services.behavior_insights()` ya decide QUÉ pasó -- eso sigue
siendo determinista, sin IA. Lo único que hace este módulo es conectar esa
lista de títulos y cuerpos en un solo mensaje, en vez de que la persona lea
seis avisos sueltos. Si Gemini no puede responder, quien llama (`apps.
notifications.services._monthly_summary_text`) cae al texto armado a mano
con los mismos datos: el resumen nunca depende de que la IA esté arriba.
"""
from __future__ import annotations

from . import models as m
from . import normalize
from .services import run

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Título corto, unas pocas palabras."},
        "body": {
            "type": "string",
            "description": (
                "El mensaje del resumen: una frase de apertura y luego 2 a 4 "
                "líneas cortas, una por idea, separadas por saltos de línea."
            ),
        },
    },
    "required": ["title", "body"],
}

SYSTEM_INSTRUCTION = """\
Redactás el resumen mensual de una app de presupuesto personal, en español \
de El Salvador y Centroamérica.

Te paso una lista de patrones de gasto YA DETECTADOS (de forma \
determinística, no los inventás ni agregás otros), cada uno con su título y \
su cuerpo. Tu trabajo es conectarlos en UN solo mensaje cercano y breve, no \
un informe. Priorizá los que más le importan a \
la persona -- no hace falta mencionar los seis si son varios.

Formato (se lee en una notificación, no de corrido): una frase corta de \
apertura y luego de 2 a 4 líneas, una por idea, cada una de máximo ~90 \
caracteres y empezando con "• ", separadas por saltos de línea. Nada de \
párrafos largos.

Reglas:
- No inventes cifras que no estén en los patrones que te paso.
- No des consejos de inversión ni de productos financieros: describí lo que \
pasó, no recomendés qué hacer con la plata.
- Tono cercano, como una nota de alguien que te conoce, no un reporte \
corporativo.
"""


def generate(*, user, workspace, insights: list[dict]) -> dict:
    """Une los patrones ya detectados en un solo texto.

    Puede levantar `AIUnavailable` (`apps.ai.client`) si Gemini no responde;
    quien llama decide el texto de respaldo. No consume cuota: el resumen lo
    dispara el servidor y no el usuario (`OP_SUMMARY` no está en
    `quotas.PLAN_FEATURE_KEYS`), aunque igual queda registrado en `AIUsage`
    para que se vea en el gasto.
    """
    payload = "\n".join(f"- {i['title']}: {i['body']}" for i in insights)
    response = run(
        user=user,
        workspace=workspace,
        operation=m.OP_SUMMARY,
        parts=[{"text": payload}],
        system_instruction=SYSTEM_INSTRUCTION,
        response_schema=RESPONSE_SCHEMA,
    )
    raw = response.json()
    return {
        "title": normalize.text(raw.get("title"), max_length=200) or "Tu resumen del mes",
        "body": normalize.text(raw.get("body"), max_length=500),
    }
