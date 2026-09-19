"""
Convertir una frase suelta en una **candidata** de transacción.

"gasté 12.50 en almuerzo con la tarjeta", "me pagaron 800 ayer", "$45 de
gasolina el lunes". Devuelve lo mismo que el escaneo de recibos — campos
editables con confianza por campo — y por la misma razón: el usuario siempre
confirma.

**Al modelo se le dan los nombres reales de las carteras y categorías del
workspace.** Sin eso, "con la tarjeta" y "gasolina" vuelven como texto libre
que después hay que adivinar a qué fila corresponde, y se falla seguido. Con
eso, el modelo elige de una lista cerrada y lo que vuelve casi siempre matchea.
Cuesta unos 150 tokens de entrada, que en Flash-Lite son centésimas de
centavo — mucho más barato que una categoría mal puesta.

La fecha relativa ("ayer", "el lunes") la resuelve el modelo, que para eso
recibe la fecha de hoy en el prompt; `normalize.date` después la acota.
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.transactions.services import find_possible_duplicates, guess_category_by_merchant

from . import models as m
from . import normalize
from .normalize import CONFIDENCE_LEVELS
from .services import run

# Cuántos nombres de cartera/categoría se le mandan al modelo. Un workspace con
# cientos de categorías haría el prompt más caro sin mejorar la elección, y las
# primeras son las que más se usan.
MAX_CONTEXT_NAMES = 60

# Frases más largas que esto no son una transacción: es alguien pegando un
# correo entero o probando qué pasa. Cortar antes del modelo acota el gasto.
MAX_TEXT_LENGTH = 500

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [Transaction.TYPE_EXPENSE, Transaction.TYPE_INCOME],
            "description": "expense si la persona gastó, income si le entró plata.",
        },
        "amount": {
            "type": "string",
            "description": "Monto, sólo dígitos y punto decimal. Vacío si no se dice.",
        },
        "currency": {"type": "string", "description": "Código ISO de 3 letras si se menciona."},
        "date": {"type": "string", "description": "Fecha en formato AAAA-MM-DD, ya resuelta."},
        "merchant": {
            "type": "string",
            "description": "Comercio o quién pagó, si se nombra. Vacío si no.",
        },
        "note": {"type": "string", "description": "El concepto en pocas palabras."},
        "wallet_hint": {
            "type": "string",
            "description": "El nombre EXACTO de una de las carteras de la lista, o vacío.",
        },
        "category_hint": {
            "type": "string",
            "description": "El nombre EXACTO de una de las categorías de la lista, o vacío.",
        },
        "confidence": {
            "type": "object",
            "properties": {
                "amount": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "date": {"type": "string", "enum": CONFIDENCE_LEVELS},
                "merchant": {"type": "string", "enum": CONFIDENCE_LEVELS},
            },
            "required": ["amount", "date", "merchant"],
        },
    },
    "required": ["type", "amount", "confidence"],
}

_SYSTEM_TEMPLATE = """\
Convertís una frase en español a los datos de una transacción de un presupuesto \
personal. Español de El Salvador y de Centroamérica: "pisto" es dinero, "colones" \
hoy son dólares, los montos suelen venir sin símbolo.

Hoy es {today} ({weekday}). Resolvé cualquier fecha relativa contra ese día: \
"ayer", "el lunes pasado", "el 3". Si la frase no dice cuándo, usá hoy y poné \
confianza "low" en la fecha.

Carteras disponibles (elegí el nombre exacto de esta lista, o dejá vacío):
{wallets}

Categorías disponibles (elegí el nombre exacto de esta lista, o dejá vacío):
{categories}

Reglas:
- No inventes. Si el monto no está en la frase, dejalo vacío con confianza \
"low". Es mucho mejor un campo vacío que un dato inventado: al vacío el \
usuario lo llena, al inventado no lo mira.
- "me pagaron", "me depositaron", "cobré", "me entró" son income. Todo lo \
demás es expense.
- `wallet_hint` y `category_hint` sólo pueden ser un nombre de las listas de \
arriba, copiado tal cual. Si nada encaja, dejalos vacíos: que el usuario elija \
es mejor que ponerle algo que no es.
- La nota es el concepto en pocas palabras ("almuerzo", "gasolina"), no la \
frase entera.
"""

_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def parse(*, user, workspace, text: str, wallet=None) -> dict:
    """Parsea la frase y devuelve la candidata. No crea nada.

    `wallet` es la cartera que el usuario ya tiene elegida en la pantalla: se
    usa para buscar duplicados cuando la frase no nombra ninguna.
    """
    text = (text or "").strip()[:MAX_TEXT_LENGTH]

    # Las carteras privadas de las que el usuario no es dueño no entran ni al
    # prompt ni al resultado: que el modelo las viera sería filtrarlas, y es el
    # mismo criterio que aplica el resto del API.
    wallets = list(
        Wallet.objects.filter(workspace=workspace, is_active=True)
        .filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))
        .order_by("name")[:MAX_CONTEXT_NAMES]
    )
    categories = list(
        Category.objects.filter(workspace=workspace, parent__isnull=False)
        .order_by("name")[:MAX_CONTEXT_NAMES]
    )

    today = timezone.localdate()
    response = run(
        user=user,
        workspace=workspace,
        operation=m.OP_PARSE,
        parts=[{"text": text}],
        system_instruction=_SYSTEM_TEMPLATE.format(
            today=today.isoformat(),
            weekday=_WEEKDAYS[today.weekday()],
            wallets=_name_list(wallets),
            categories=_name_list(categories),
        ),
        response_schema=RESPONSE_SCHEMA,
    )
    raw = response.json()

    txn_type = _type(raw.get("type"))
    amount = normalize.amount(raw.get("amount"))
    date = normalize.date(raw.get("date"), today=today)
    merchant = normalize.text(raw.get("merchant"))
    note = normalize.text(raw.get("note")) or merchant

    resolved_wallet, wallet_source = _resolve_wallet(wallets, raw.get("wallet_hint"))
    category, category_source = _resolve_category(
        workspace=workspace, txn_type=txn_type, merchant=merchant,
        hint=raw.get("category_hint"), categories=categories,
    )

    unusable = []
    if amount is None:
        unusable.append("amount")
    if not raw.get("date"):
        unusable.append("date")

    return {
        "type": txn_type,
        "amount": amount,
        "currency": normalize.currency(raw.get("currency")),
        "date": date,
        "merchant": merchant,
        "description": note,
        "wallet": resolved_wallet.id if resolved_wallet else None,
        "wallet_source": wallet_source,
        "category": category.id if category else None,
        "category_source": category_source,
        "confidence": normalize.confidence(
            raw.get("confidence"), fields=("amount", "date", "merchant"), unusable=unusable
        ),
        "possible_duplicates": _duplicates(resolved_wallet or wallet, amount, date),
    }


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------
def _name_list(rows):
    return "\n".join(f"- {row.name}" for row in rows) or "- (ninguna)"


def _type(value):
    return value if value in (Transaction.TYPE_EXPENSE, Transaction.TYPE_INCOME) else (
        Transaction.TYPE_EXPENSE
    )


def _resolve_wallet(wallets, hint):
    """La cartera que nombró la frase, si es una de las que le pasamos.

    Se matchea contra la misma lista que vio el modelo — nunca contra la base
    de nuevo — así que no hay forma de que devuelva una cartera que el usuario
    no podía ver.
    """
    hint = (hint or "").strip()
    if not hint:
        return None, None
    lowered = hint.casefold()
    for wallet in wallets:
        if wallet.name.casefold() == lowered:
            return wallet, "text"
    return None, None


def _resolve_category(*, workspace, txn_type, merchant, hint, categories):
    """Mismo orden que en los recibos: historial primero, modelo después."""
    from_history = guess_category_by_merchant(
        workspace=workspace, txn_type=txn_type, merchant=merchant
    )
    if from_history is not None:
        return from_history, "history"

    hint = (hint or "").strip()
    if not hint:
        return None, None
    lowered = hint.casefold()
    for category in categories:
        if category.name.casefold() == lowered and category.type == txn_type:
            return category, "ai"
    return None, None


def _duplicates(wallet, amount, date):
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
