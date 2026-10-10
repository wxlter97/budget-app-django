"""
Leer un estado de cuenta (PDF o foto) y devolver una **candidata**.

Misma regla que `receipts.py`: la IA propone, el usuario confirma. Acá no se
crea ninguna cartera ni transacción; la app muestra lo leído (con la confianza
por campo) y recién al confirmar se crea lo que el usuario eligió.

Salen dos cosas del mismo documento:
- `wallet`: los datos para crear la cartera (tarjeta o cuenta) con campos
  prellenados -- banco, últimos 4, límite, día de corte y de pago, mínimo…
- `transactions`: los movimientos del período, con categoría sugerida por el
  historial del workspace y duplicados marcados contra la cartera elegida.
"""
from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal, InvalidOperation

from apps.transactions.models import Transaction
from apps.transactions.services import find_possible_duplicates, guess_category_by_merchant

from . import models as m
from . import normalize
from .normalize import CONFIDENCE_LEVELS
from .services import run

MAX_TRANSACTIONS = 200

_KIND_CHOICES = ["credit_card", "bank_account", "other"]
_DIRECTIONS = ["expense", "income"]

# Los montos van como texto por la misma razón que en recibos: un float en el
# JSON se redondea antes de llegar a Decimal.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_kind": {"type": "string", "enum": _KIND_CHOICES},
        "bank": {"type": "string", "description": "Nombre del banco emisor."},
        "product": {
            "type": "string",
            "description": "Nombre del producto (p. ej. 'Visa Platinum', 'Cuenta de ahorro').",
        },
        "last4": {"type": "string", "description": "Últimos 4 dígitos de la tarjeta o cuenta."},
        "currency": {"type": "string", "description": "Código ISO de 3 letras."},
        "period_start": {"type": "string", "description": "Inicio del período, AAAA-MM-DD."},
        "period_end": {"type": "string", "description": "Fecha de corte / fin del período, AAAA-MM-DD."},
        "payment_due_date": {"type": "string", "description": "Fecha límite de pago, AAAA-MM-DD."},
        "closing_balance": {
            "type": "string",
            "description": "Saldo al corte: lo que se debe (tarjeta) o lo que hay (cuenta). Solo dígitos y punto.",
        },
        "minimum_payment": {"type": "string", "description": "Pago mínimo, si aparece."},
        "credit_limit": {"type": "string", "description": "Límite de crédito, si aparece."},
        "annual_interest_rate": {"type": "string", "description": "Tasa de interés anual en %, si aparece."},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "AAAA-MM-DD."},
                    "description": {"type": "string"},
                    "amount": {"type": "string", "description": "Monto en positivo, solo dígitos y punto."},
                    "direction": {
                        "type": "string",
                        "enum": _DIRECTIONS,
                        "description": "expense = compra/cargo/retiro; income = pago, abono, depósito, devolución.",
                    },
                },
                "required": ["date", "description", "amount", "direction"],
            },
        },
        "confidence": {
            "type": "object",
            "properties": {
                "bank": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "last4": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "closing_balance": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "period_end": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "transactions": {"type": "string", "enum": CONFIDENCE_LEVELS},
            },
            "required": ["bank", "closing_balance", "transactions"],
        },
    },
    "required": ["statement_kind", "confidence"],
}

SYSTEM_INSTRUCTION = """\
Extraés datos de estados de cuenta bancarios (tarjetas de crédito y cuentas), \
casi siempre de El Salvador y en dólares. Recibís un PDF o una foto.

Reglas:
- No inventes. Un dato que no aparece va vacío, con confianza "low". Es mucho \
mejor un campo vacío que uno inventado: al vacío el usuario lo llena, al \
inventado no lo mira.
- Montos siempre en positivo y como texto. El sentido va en `direction`: \
compras, cargos, comisiones, intereses y retiros son "expense"; pagos, abonos, \
depósitos y devoluciones son "income".
- En una tarjeta de crédito, `closing_balance` es lo que se debe al corte \
("saldo actual", "total a pagar"), no el pago mínimo ni el límite.
- Las fechas del período y de pago van completas (AAAA-MM-DD); deducí el año \
del encabezado del documento si las líneas traen sólo día y mes.
- No repitas movimientos ni incluyas totales, subtotales ni saldos corridos \
como si fueran movimientos.
- Confianza "low" también cuando el dato se lee a medias.
"""


def scan(*, user, workspace, file_bytes: bytes, content_type: str, wallet=None) -> dict:
    response = run(
        user=user,
        workspace=workspace,
        operation=m.OP_STATEMENT,
        parts=[
            {"inline_data": {"mime_type": content_type, "data": base64.b64encode(file_bytes).decode()}},
            {"text": "Extraé los datos de este estado de cuenta."},
        ],
        system_instruction=SYSTEM_INSTRUCTION,
        response_schema=RESPONSE_SCHEMA,
    )
    return build_candidate(response.json(), workspace=workspace, wallet=wallet)


def build_candidate(raw: dict, *, workspace, wallet=None) -> dict:
    """Normaliza lo que dijo el modelo. Separado de `scan` para probarlo sin
    llamar a Gemini."""
    raw = raw if isinstance(raw, dict) else {}

    kind = raw.get("statement_kind")
    kind = kind if kind in _KIND_CHOICES else "other"

    period_end = _date(raw.get("period_end"))
    payment_due = _date(raw.get("payment_due_date"))
    closing_balance = _amount(raw.get("closing_balance"), allow_zero=True)

    txns = _transactions(raw.get("transactions"), workspace=workspace, wallet=wallet)

    wallet_data = {
        "kind": "credit" if kind == "credit_card" else "bank",
        "purpose": "debt" if kind == "credit_card" else "spending",
        "name": _wallet_name(raw),
        "bank": normalize.text(raw.get("bank"), max_length=100) or None,
        "card_last4": _last4(raw.get("last4")),
        "currency": normalize.currency(raw.get("currency")) or "USD",
        "credit_limit": _amount(raw.get("credit_limit")),
        "billing_cycle_day": period_end.day if (kind == "credit_card" and period_end) else None,
        "payment_due_day": payment_due.day if (kind == "credit_card" and payment_due) else None,
        "minimum_payment": _amount(raw.get("minimum_payment")),
        "interest_rate": _rate(raw.get("annual_interest_rate")),
        "closing_balance": closing_balance,
    }

    return {
        "statement_kind": kind,
        "period_start": _date(raw.get("period_start")),
        "period_end": period_end,
        "payment_due_date": payment_due,
        "wallet": wallet_data,
        "transactions": txns,
        "confidence": _confidence(raw.get("confidence"), wallet_data, period_end, txns),
    }


# ---------------------------------------------------------------------------
# Normalización propia (lo común vive en `normalize.py`)
# ---------------------------------------------------------------------------
def _amount(value, *, allow_zero=False):
    if allow_zero and str(value or "").replace("$", "").replace(",", "").strip() in ("0", "0.0", "0.00"):
        return Decimal("0.00")
    return normalize.amount(value)


def _rate(value):
    """Tasa anual en %, entre 0 y 200: fuera de eso casi seguro es un error de
    lectura (una tasa mensual, un monto)."""
    cleaned = str(value or "").replace("%", "").replace(",", "").strip()
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None
    if parsed <= 0 or parsed > 200:
        return None
    return parsed.quantize(Decimal("0.01"))


def _date(value):
    """Fecha ISO o `None`. A diferencia de `normalize.date` NO cae a hoy ni
    rechaza futuras: la fecha límite de pago casi siempre está en el futuro, y
    inventar "hoy" en un corte que no se leyó sería peor que dejarlo vacío."""
    try:
        parsed = dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if dt.date(2000, 1, 1) <= parsed <= dt.date(2100, 1, 1) else None


def _last4(value):
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _wallet_name(raw):
    bank = normalize.text(raw.get("bank"), max_length=60)
    product = normalize.text(raw.get("product"), max_length=60)
    last4 = _last4(raw.get("last4"))
    name = " ".join(p for p in (bank, product) if p) or "Cartera nueva"
    return f"{name} ···· {last4}" if last4 else name


def _transactions(raw, *, workspace, wallet):
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for row in raw[:MAX_TRANSACTIONS]:
        if not isinstance(row, dict):
            continue
        amount = normalize.amount(row.get("amount"))
        date = _date(row.get("date"))
        description = normalize.text(row.get("description"))
        if amount is None or date is None or not description:
            continue
        direction = row.get("direction") if row.get("direction") in _DIRECTIONS else "expense"
        key = (date, description.lower(), amount, direction)
        # El modelo a veces repite una línea que cruza de página; dos cargos
        # idénticos el mismo día son raros, y el usuario lo ve marcado igual.
        if key in seen:
            continue
        seen.add(key)

        txn_type = Transaction.TYPE_INCOME if direction == "income" else Transaction.TYPE_EXPENSE
        category = guess_category_by_merchant(
            workspace=workspace, txn_type=txn_type, merchant=description
        )
        out.append({
            "date": date,
            "description": description,
            "amount": amount,
            "type": txn_type,
            "category": category.id if category else None,
            "possible_duplicates": _duplicates(wallet, amount, date),
        })
    return out


def _duplicates(wallet, amount, date):
    if wallet is None:
        return []
    return [
        {"id": t.id, "date": t.date, "amount": t.amount, "description": t.description}
        for t in find_possible_duplicates(wallet=wallet, amount=amount, date=date)[:3]
    ]


def _confidence(raw, wallet_data, period_end, txns):
    raw = raw if isinstance(raw, dict) else {}
    unusable = []
    if wallet_data["closing_balance"] is None:
        unusable.append("closing_balance")
    if not wallet_data["bank"]:
        unusable.append("bank")
    if period_end is None:
        unusable.append("period_end")
    if wallet_data["card_last4"] is None:
        unusable.append("last4")
    if not txns:
        unusable.append("transactions")
    return normalize.confidence(
        raw,
        fields=("bank", "last4", "closing_balance", "period_end", "transactions"),
        unusable=unusable,
    )
