"""
Leer un recibo y devolver una **candidata**, nunca una transacción.

La regla de esta función es que el usuario siempre confirma. La IA acierta
bastante y se equivoca poco, pero cuando se equivoca lo hace con la misma
seguridad con la que acierta, y acá lo que está en juego es el saldo de
alguien. Por eso todo sale como algo editable y con un nivel de confianza por
campo, y por eso el recibo se guarda como `Transaction.receipt` recién cuando
el usuario aprieta guardar — no antes.

Orden en el que se resuelve la categoría, que importa: **primero el historial
del workspace** (`guess_category_by_merchant`, que es gratis, determinista y
sabe cómo categorizó *esta* gente *este* comercio antes) y sólo si eso no
acierta, la sugerencia del modelo. Nunca al revés.
"""
from __future__ import annotations

import base64

from apps.transactions.models import Category, Transaction
from apps.transactions.services import find_possible_duplicates, guess_category_by_merchant

from . import models as m
from . import normalize
from .normalize import CONFIDENCE_LEVELS
from .services import run

# Esquema de salida estructurada. Los montos se piden como **texto** y no como
# número a propósito: un JSON con `12.50` vuelve como float y de ahí a Decimal
# hay un redondeo que no tiene por qué existir cuando se trata de plata.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "total": {
            "type": "string",
            "description": "Total pagado, sólo dígitos y punto decimal. Vacío si no se lee.",
        },
        "tax": {
            "type": "string",
            "description": "IVA o impuesto desglosado, mismo formato. Vacío si no aparece.",
        },
        "currency": {"type": "string", "description": "Código ISO de 3 letras, p. ej. USD."},
        "date": {"type": "string", "description": "Fecha del recibo en formato AAAA-MM-DD."},
        "merchant": {"type": "string", "description": "Nombre del comercio, sin razón social."},
        "category_hint": {
            "type": "string",
            "description": "En qué categoría de gasto cae, en una o dos palabras.",
        },
        "items": {
            "type": "array",
            "description": "Líneas del detalle, si el recibo las trae.",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "string"},
                    "amount": {"type": "string"},
                },
                "required": ["description", "amount"],
            },
        },
        "confidence": {
            "type": "object",
            "description": "Qué tan seguro estás de cada campo.",
            "properties": {
                "total": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "date": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "merchant": {"type": "string", "enum": CONFIDENCE_LEVELS},
            },
            "required": ["total", "date", "merchant"],
        },
    },
    "required": ["total", "confidence"],
}

SYSTEM_INSTRUCTION = """\
Extraés datos de recibos y facturas. Muchos son de El Salvador: tickets de \
supermercado, facturas de consumidor final y documentos tributarios \
electrónicos (DTE), casi siempre en dólares y con IVA del 13% incluido en el \
precio mostrado.

Reglas:
- El total es lo que la persona pagó de verdad: si el recibo muestra subtotal, \
IVA y total, el total es el último. Si hay propina sumada aparte, incluila.
- No inventes. Un campo que no se lee va vacío, con confianza "low". Es mucho \
mejor un campo vacío que un dato inventado: al vacío el usuario lo llena, al \
inventado no lo mira.
- La fecha es la del consumo, no la de impresión si son distintas.
- La confianza es "low" también cuando el dato se lee a medias (foto \
borrosa, papel cortado, cifra ambigua).
"""


def scan(*, user, workspace, file_bytes: bytes, content_type: str, wallet=None) -> dict:
    """Lee el recibo y devuelve la candidata lista para que la app la muestre.

    No toca la base más que para leer (categorías e historial): no crea la
    transacción ni guarda el archivo.
    """
    response = run(
        user=user,
        workspace=workspace,
        operation=m.OP_RECEIPT,
        parts=[
            {"inline_data": {"mime_type": content_type, "data": base64.b64encode(file_bytes).decode()}},
            {"text": "Extraé los datos de este recibo."},
        ],
        system_instruction=SYSTEM_INSTRUCTION,
        response_schema=RESPONSE_SCHEMA,
    )
    raw = response.json()

    amount = normalize.amount(raw.get("total"))
    date = normalize.date(raw.get("date"))
    merchant = normalize.text(raw.get("merchant"))
    confidence = _confidence(raw.get("confidence"), amount=amount, date_was_read=bool(raw.get("date")))

    category, category_source = _resolve_category(
        workspace=workspace, merchant=merchant, hint=raw.get("category_hint")
    )

    return {
        "amount": amount,
        "tax_amount": normalize.amount(raw.get("tax")),
        "currency": normalize.currency(raw.get("currency")),
        "date": date,
        "merchant": merchant,
        "description": merchant,
        "category": category.id if category else None,
        "category_source": category_source,
        "items": _items(raw.get("items")),
        "confidence": confidence,
        "possible_duplicates": _duplicates(wallet, amount, date),
    }


# ---------------------------------------------------------------------------
# Normalización propia del recibo (lo común vive en `normalize.py`)
# ---------------------------------------------------------------------------
def _confidence(raw, *, amount, date_was_read):
    """La confianza del modelo, con dos correcciones nuestras: un monto que no
    se pudo usar y una fecha que no se pudo leer quedan en `low` diga lo que
    diga él."""
    unusable = []
    if amount is None:
        unusable.append("total")
    if not date_was_read:
        unusable.append("date")
    result = normalize.confidence(
        raw, fields=("total", "date", "merchant"), unusable=unusable
    )
    # `total` es el nombre del campo en el recibo; en la app el campo se llama
    # `amount`. Se traduce acá para no filtrar el vocabulario del prompt al API.
    result["amount"] = result.pop("total")
    return result


def _items(raw):
    if not isinstance(raw, list):
        return []
    items = []
    for row in raw[:50]:  # un ticket de súper largo no debería inflar la respuesta
        if not isinstance(row, dict):
            continue
        description = normalize.text(row.get("description"))
        if not description:
            continue
        items.append({
            "description": description,
            "quantity": (str(row.get("quantity") or "").strip() or None),
            "amount": normalize.amount(row.get("amount")),
        })
    return items


def _resolve_category(*, workspace, merchant, hint):
    """Historial primero, IA después. Devuelve `(Category | None, origen)`."""
    from_history = guess_category_by_merchant(
        workspace=workspace, txn_type=Transaction.TYPE_EXPENSE, merchant=merchant
    )
    if from_history is not None:
        return from_history, "history"

    hint = (hint or "").strip()
    if not hint:
        return None, None
    # Sólo categorías asignables (hoja, del tipo correcto): sugerir una
    # categoría padre haría que la transacción no se pueda guardar.
    match = Category.objects.filter(
        workspace=workspace,
        type=Transaction.TYPE_EXPENSE,
        parent__isnull=False,
        name__iexact=hint,
    ).first()
    return (match, "ai") if match else (None, None)


def _duplicates(wallet, amount, date):
    """"Esto parece que ya lo registraste" — la misma detección que usan el
    alta rápida y la importación por correo. Sin cartera no hay contra qué
    comparar, así que se devuelve vacío."""
    if wallet is None or amount is None:
        return []
    return [
        {
            "id": txn.id,
            "date": txn.date,
            "amount": txn.amount,
            "description": txn.description,
        }
        for txn in find_possible_duplicates(wallet=wallet, amount=amount, date=date)[:5]
    ]
