"""Agregados de lealtad para el reporte del workspace (ver `api.py`) y el
reconocimiento de comercios en la descripción de una transacción."""
import re
import unicodedata
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import LoyaltyEarning, LoyaltyMovement, LoyaltyProgram, Merchant


def normalize_text(text: str) -> str:
    """Minúsculas, sin tildes y con todo lo que no sea letra o número como un
    espacio: "McDonald's — Metrocentro" -> "mcdonald s metrocentro". La misma
    regla vive en el front (`lib/loyaltyRate.ts`); si se cambia una, la otra."""
    stripped = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def match_merchant(description: str, merchants=None):
    """El comercio (`Merchant`) que aparece en la descripción, o `None`.

    Un alias cuenta sólo como palabra(s) completa(s) (así "mc" no dispara con
    "mcdonalds" ni "uno" con "alguno"); si varios coinciden gana el alias más
    largo ("uber eats" antes que "uber"). `merchants` permite pasar la lista ya
    cargada cuando se reconocen muchas descripciones seguidas."""
    text = f" {normalize_text(description)} "
    if not text.strip():
        return None
    best, best_len = None, 0
    for merchant in merchants if merchants is not None else Merchant.objects.all():
        for alias in merchant.alias_list:
            needle = normalize_text(alias)
            if len(needle) > best_len and f" {needle} " in text:
                best, best_len = merchant, len(needle)
    return best


def points_balances(workspace) -> list[dict]:
    """Disponible de puntos por cartera + programa (ganado + ajustes - canjes)."""
    result = []
    for wallet in wallet_balances(workspace):
        for p in wallet["programs"]:
            if p["kind"] != LoyaltyProgram.KIND_POINTS or not (p["available"] or p["earned"]):
                continue
            result.append({
                "wallet": wallet["wallet"], "wallet_name": wallet["wallet_name"],
                "program": p["program"], "program_name": p["name"] or "Puntos",
                "points": p["available"], "estimated_value": p["estimated_value"],
            })
    return result


def period_totals(workspace, date_after=None, date_before=None) -> list[dict]:
    """Cashback ganado y descuento ahorrado por cartera, en el período dado."""
    qs = LoyaltyEarning.objects.filter(
        workspace=workspace, kind__in=[LoyaltyProgram.KIND_CASHBACK, LoyaltyProgram.KIND_DISCOUNT]
    ).select_related("transaction", "transaction__wallet")
    if date_after:
        qs = qs.filter(transaction__date__gte=date_after)
    if date_before:
        qs = qs.filter(transaction__date__lte=date_before)

    buckets: dict = {}
    for earning in qs:
        wallet = earning.transaction.wallet
        bucket = buckets.setdefault(
            wallet.id,
            {
                "wallet": wallet.id,
                "wallet_name": wallet.name,
                "cashback_earned": Decimal("0"),
                "discount_saved": Decimal("0"),
            },
        )
        if earning.kind == LoyaltyProgram.KIND_CASHBACK:
            bucket["cashback_earned"] += earning.amount or Decimal("0")
        else:
            bucket["discount_saved"] += earning.discount_saved_amount or Decimal("0")
    return sorted(buckets.values(), key=lambda b: b["wallet_name"])


def loyalty_summary(workspace, date_after=None, date_before=None) -> dict:
    return {
        "wallets": wallet_balances(workspace),
        "points_balances": points_balances(workspace),
        "period_totals": period_totals(workspace, date_after, date_before),
    }


def map_categories_to_rubros(categories) -> list:
    """Asigna el rubro de lealtad (`CategoryType`) a las categorías que **no tienen
    uno todavía**, según su nombre (`catalog.CATEGORY_TO_RUBRO`, sin importar
    mayúsculas ni tildes). Nunca pisa un rubro elegido a mano. Devuelve las
    categorías que cambió. Si los rubros no están cargados todavía
    (`seed_loyalty_catalog`), no hace nada."""
    from . import catalog
    from .models import CategoryType

    rubros = {t.slug: t for t in CategoryType.objects.filter(slug__in=set(catalog.CATEGORY_TO_RUBRO.values()))}
    changed = []
    for category in categories.filter(category_type__isnull=True):
        rubro = rubros.get(catalog.CATEGORY_TO_RUBRO.get(normalize_text(category.name)))
        if rubro is not None:
            category.category_type = rubro
            category.save(update_fields=["category_type", "updated_at"])
            changed.append(category)
    return changed


def recompute_earnings(transactions) -> int:
    """Vuelve a calcular lo ganado (puntos y cashback) de esas transacciones con
    las reglas actuales: sirve cuando se cargó o cambió el catálogo, o se mapeó
    un rubro, DESPUÉS de que los gastos ya existían (la señal de
    `signals.py` sólo corre al guardar). No toca los descuentos, que registra el
    cliente. Devuelve cuántas transacciones recorrió."""
    from .signals import _recompute

    count = 0
    for txn in transactions.select_related("wallet", "category"):
        _recompute(txn)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Disponible, canjes y ajustes
# ---------------------------------------------------------------------------
_BALANCE_KINDS = (LoyaltyProgram.KIND_POINTS, LoyaltyProgram.KIND_CASHBACK)
ZERO = Decimal("0")


def _unit(kind: str) -> str:
    return "points" if kind == LoyaltyProgram.KIND_POINTS else "currency"


def earned_by_program(workspace, wallet_id=None) -> dict:
    """`{(wallet_id, program_id): ganado}` en la unidad del programa: puntos, o dinero
    para cashback (el descuento no acumula saldo)."""
    qs = LoyaltyEarning.objects.filter(workspace=workspace, kind__in=_BALANCE_KINDS)
    if wallet_id:
        qs = qs.filter(transaction__wallet_id=wallet_id)
    rows = qs.values("transaction__wallet_id", "program_id", "kind").annotate(
        points=Sum("points"), amount=Sum("amount")
    )
    out = {}
    for row in rows:
        value = row["points"] if row["kind"] == LoyaltyProgram.KIND_POINTS else row["amount"]
        out[(row["transaction__wallet_id"], row["program_id"])] = value or ZERO
    return out


def movements_by_program(workspace, wallet_id=None) -> dict:
    """`{(wallet_id, program_id): {"redeemed": x, "adjusted": y}}`, ambos como número
    positivo o con signo según corresponda (ver `LoyaltyMovement.delta`)."""
    qs = LoyaltyMovement.objects.filter(workspace=workspace)
    if wallet_id:
        qs = qs.filter(wallet_id=wallet_id)
    out: dict = {}
    for row in qs.values("wallet_id", "program_id", "kind").annotate(total=Sum("delta")):
        bucket = out.setdefault((row["wallet_id"], row["program_id"]), {"redeemed": ZERO, "adjusted": ZERO})
        if row["kind"] == LoyaltyMovement.KIND_REDEEM:
            bucket["redeemed"] += -(row["total"] or ZERO)
        else:
            bucket["adjusted"] += row["total"] or ZERO
    return out


def available_for(workspace, wallet_id, program_id) -> Decimal:
    """Lo que hoy se puede canjear de un programa en una cartera."""
    earned = earned_by_program(workspace, wallet_id).get((wallet_id, program_id), ZERO)
    moved = movements_by_program(workspace, wallet_id).get((wallet_id, program_id), {})
    return earned + moved.get("adjusted", ZERO) - moved.get("redeemed", ZERO)


def wallet_balances(workspace) -> list[dict]:
    """Una entrada por tarjeta con producto: sus programas de puntos y cashback con
    ganado / ajustado / canjeado / disponible, y lo ahorrado en descuentos. Es lo que
    muestra "Recompensas": cada tarjeta por separado, sin mezclar bancos."""
    from apps.accounts.models import Wallet

    earned = earned_by_program(workspace)
    moved = movements_by_program(workspace)
    saved = {}
    for e in LoyaltyEarning.objects.filter(workspace=workspace, kind=LoyaltyProgram.KIND_DISCOUNT).select_related("transaction"):
        saved[e.transaction.wallet_id] = saved.get(e.transaction.wallet_id, ZERO) + (e.discount_saved_amount or ZERO)

    wallets = (
        Wallet.objects.filter(workspace=workspace, card_product__isnull=False)
        .select_related("card_product__bank")
        .prefetch_related("card_product__programs")
        .order_by("card_product__bank__name", "name")
    )
    result = []
    for wallet in wallets:
        programs = []
        for program in wallet.card_product.programs.all():
            if program.kind not in _BALANCE_KINDS:
                continue
            key = (wallet.id, program.id)
            got = earned.get(key, ZERO)
            mv = moved.get(key, {"redeemed": ZERO, "adjusted": ZERO})
            # Un programa desactivado sólo se muestra si todavía tiene historia.
            if not program.is_active and not (got or mv["redeemed"] or mv["adjusted"]):
                continue
            available = got + mv["adjusted"] - mv["redeemed"]
            if program.kind == LoyaltyProgram.KIND_CASHBACK:
                value = available
            else:
                value = available * program.point_value if program.point_value is not None else None
            programs.append({
                "program": program.id, "name": program.name or program.get_kind_display(),
                "kind": program.kind, "unit": _unit(program.kind), "is_active": program.is_active,
                "earned": got, "adjusted": mv["adjusted"], "redeemed": mv["redeemed"],
                "available": available, "point_value": program.point_value,
                "estimated_value": value, "min_amount": program.min_amount,
            })
        result.append({
            "wallet": wallet.id, "wallet_name": wallet.name, "currency": wallet.currency,
            "bank": wallet.card_product.bank_id, "bank_name": wallet.card_product.bank.name,
            "product_name": wallet.card_product.name, "programs": programs,
            "discount_saved": saved.get(wallet.id, ZERO),
            # Suma del valor en dinero de lo disponible (los puntos sin valor de canje no cuentan).
            "total_value": sum((p["estimated_value"] for p in programs if p["estimated_value"] is not None), ZERO),
        })
    return result


def get_or_create_rewards_category(workspace):
    """La categoría de ingreso "Recompensas" donde se registran los canjes depositados en
    una cartera (misma idea que `get_or_create_refund_category`)."""
    from apps.transactions.models import Category

    existing = Category.objects.filter(
        workspace=workspace, type=Category.TYPE_INCOME, name__iexact="Recompensas"
    ).first()
    if existing:
        return existing
    group, _ = Category.objects.get_or_create(
        workspace=workspace, name="Ingresos", type=Category.TYPE_INCOME, parent=None,
        defaults={"icon": "💰", "color": "#22C55E"},
    )
    return Category.objects.create(
        workspace=workspace, name="Recompensas", type=Category.TYPE_INCOME, parent=group,
        icon="🎁", color="#22C55E",
    )


def _check_target(workspace, wallet, program):
    if wallet.workspace_id != workspace.id:
        raise ValidationError({"wallet": "No es una cartera de este espacio de trabajo."})
    if program.kind not in _BALANCE_KINDS:
        raise ValidationError({"program": "Este programa no acumula saldo (los descuentos se aplican al pagar)."})
    if wallet.card_product_id != program.card_product_id:
        raise ValidationError({"program": "Este programa no es de la tarjeta elegida."})


def redeem(*, workspace, wallet, program, quantity: Decimal, date=None, note="", cash_value=None,
           deposit_wallet=None, user=None) -> LoyaltyMovement:
    """Canjea `quantity` (puntos, o dinero si es cashback): baja el disponible y, si se
    indica `deposit_wallet`, registra el ingreso en esa cartera por `cash_value`. Un
    punto vale distinto el día del canje, así que el valor se puede fijar a mano; si
    no, se usa el valor de canje del programa."""
    from apps.transactions.models import Transaction

    _check_target(workspace, wallet, program)
    if quantity <= 0:
        raise ValidationError({"quantity": "Tiene que ser mayor a cero."})
    available = available_for(workspace, wallet.id, program.id)
    if quantity > available:
        raise ValidationError({"quantity": f"Sólo tienes {available} disponible."})

    if cash_value is None:
        if program.kind == LoyaltyProgram.KIND_CASHBACK:
            cash_value = quantity
        elif program.point_value is not None:
            cash_value = (quantity * program.point_value).quantize(Decimal("0.01"))
    if deposit_wallet is not None:
        if deposit_wallet.workspace_id != workspace.id:
            raise ValidationError({"deposit_wallet": "No es una cartera de este espacio de trabajo."})
        if cash_value is None or cash_value <= 0:
            raise ValidationError({"cash_value": "Indica cuánto valió el canje para poder depositarlo."})

    date = date or timezone.localdate()
    with db_transaction.atomic():
        deposit = None
        if deposit_wallet is not None:
            deposit = Transaction.objects.create(
                type=Transaction.TYPE_INCOME, wallet=deposit_wallet,
                category=get_or_create_rewards_category(workspace), amount=cash_value,
                description=f"Canje: {program.name or wallet.card_product.name}", date=date,
                created_by=user,
            )
        return LoyaltyMovement.objects.create(
            workspace=workspace, wallet=wallet, program=program, kind=LoyaltyMovement.KIND_REDEEM,
            delta=-quantity, cash_value=cash_value, date=date, note=note,
            deposit_transaction=deposit, created_by=user,
        )


def adjust(*, workspace, wallet, program, quantity: Decimal, date=None, note="", user=None) -> LoyaltyMovement:
    """Corrige el disponible sumando (`quantity` > 0) o restando (< 0). Nunca sobrescribe
    el saldo: queda como un renglón con su motivo. No puede dejar el disponible en negativo."""
    _check_target(workspace, wallet, program)
    if quantity == 0:
        raise ValidationError({"quantity": "El ajuste no puede ser cero."})
    if available_for(workspace, wallet.id, program.id) + quantity < 0:
        raise ValidationError({"quantity": "El ajuste dejaría el disponible en negativo."})
    return LoyaltyMovement.objects.create(
        workspace=workspace, wallet=wallet, program=program, kind=LoyaltyMovement.KIND_ADJUST,
        delta=quantity, date=date or timezone.localdate(), note=note, created_by=user,
    )


def update_movement(movement: LoyaltyMovement, *, quantity=None, date=None, note=None) -> LoyaltyMovement:
    """Corrige un movimiento. La cantidad sólo se edita en un ajuste (un canje tiene
    un ingreso detrás: se deshace y se vuelve a hacer)."""
    if quantity is not None:
        if movement.kind != LoyaltyMovement.KIND_ADJUST:
            raise ValidationError({"quantity": "Un canje no se edita: deshazlo y vuelve a canjear."})
        if quantity == 0:
            raise ValidationError({"quantity": "El ajuste no puede ser cero."})
        others = available_for(movement.workspace, movement.wallet_id, movement.program_id) - movement.delta
        if others + quantity < 0:
            raise ValidationError({"quantity": "Ya canjeaste recompensas que dependen de este ajuste."})
        movement.delta = quantity
    if date is not None:
        movement.date = date
    if note is not None:
        movement.note = note
    movement.save()
    return movement


def undo_movement(movement: LoyaltyMovement) -> None:
    """Deshace un movimiento. En un canje depositado también se elimina el ingreso que
    registró, para que la cartera y el libro no queden desalineados. No deja deshacer un
    ajuste positivo si con eso el disponible quedaría en negativo (ya se canjeó)."""
    if movement.delta > 0:
        remaining = available_for(movement.workspace, movement.wallet_id, movement.program_id) - movement.delta
        if remaining < 0:
            raise ValidationError({"detail": "Ya canjeaste recompensas que dependen de este ajuste."})
    with db_transaction.atomic():
        if movement.deposit_transaction_id:
            movement.deposit_transaction.soft_delete()
        movement.soft_delete()
