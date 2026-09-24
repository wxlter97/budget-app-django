"""Generación automática de transacciones (gastos recurrentes) y aritmética
de compras a plazo.

`generate_recurring_transactions` es idempotente respecto a
`RecurringExpense.next_due_date`: correrla dos veces el mismo día no duplica
nada. Las compras a plazo (`InstallmentPurchase`) ya no generan
transacciones por cuota -- `installment_amounts` solo calcula montos para el
estado de cuenta (ver `apps.accounts.services.installment_status`).
"""
import datetime as dt
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from rest_framework.exceptions import ValidationError

from .models import Category, Person, RecurringExpense, Transaction, TransactionShare

# Qué archivo vale como recibo. Vive acá y no en la vista porque hay dos
# puertas por las que entra el mismo archivo — subirlo a una transacción
# (`/transactions/{id}/receipt/`) y mandarlo a leer por IA
# (`/ai/receipt/`) — y aceptar en una lo que la otra rechaza sería una
# sorpresa fea: el usuario escanea, ve los datos, confirma, y recién ahí
# falla la subida.
RECEIPT_MAX_SIZE = 8 * 1024 * 1024  # 8 MB
RECEIPT_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf",
}


def guess_category_by_merchant(*, workspace, txn_type, merchant):
    """
    Adivina la ``Category`` más probable para un comercio, mirando cómo se
    categorizó ese mismo comercio antes: la categoría más frecuente entre
    transacciones pasadas del workspace cuya descripción lo contenga.
    ``None`` si no hay comercio o no hay historial suficiente.

    Usado por el alta rápida de Apple Shortcuts (``apps.quickadd``) y por la
    confirmación de importación de correos bancarios (``apps.email_import``)
    -- mismo comercio, misma forma de adivinar en los dos lugares.
    """
    if not merchant:
        return None

    assignable = Category.objects.filter(workspace=workspace, type=txn_type, parent__isnull=False)

    best = (
        Transaction.objects.filter(
            wallet__workspace=workspace,
            type=txn_type,
            category__isnull=False,
            description__icontains=merchant,
        )
        .values("category")
        .annotate(n=Count("category"))
        .order_by("-n")
        .first()
    )
    if not best:
        return None
    return assignable.filter(pk=best["category"]).first()


def visible_transactions(workspace, user):
    """
    Transacciones del workspace que puede ver ``user``: las de carteras
    compartidas + las de carteras privadas de las que es owner.
    """
    from apps.accounts.models import Wallet

    return Transaction.objects.filter(wallet__workspace=workspace).filter(
        Q(wallet__visibility=Wallet.VISIBILITY_SHARED) | Q(wallet__owner=user)
    )


# ---------------------------------------------------------------------------
# Categorías por defecto (grupo → categoría, estilo Buddy, en es)
# ---------------------------------------------------------------------------
# grupo: (nombre, icono, color, [ (categoría, icono, color), ... ])
DEFAULT_EXPENSE_CATEGORY_GROUPS = [
    ("Vivienda", "🏠", "#F59E0B", [
        ("Alquiler / Préstamo", "🏦", "#F59E0B"),
        ("Internet", "📶", "#F59E0B"),
        ("Electricidad", "⚡", "#F59E0B"),
        ("Agua", "💧", "#F59E0B"),
        ("Teléfono", "📱", "#F59E0B"),
        ("Mantenimiento", "🔧", "#F59E0B"),
    ]),
    ("Comida", "🍽", "#3B82F6", [
        ("Comida", "🍽", "#3B82F6"),
        ("Supermercado", "🛒", "#3B82F6"),
        ("Restaurantes", "🍔", "#3B82F6"),
    ]),
    ("Transporte", "🚗", "#8B5CF6", [
        ("Gasolina", "⛽", "#8B5CF6"),
        ("Transporte público", "🚌", "#8B5CF6"),
        ("Parking", "🅿️", "#8B5CF6"),
        ("Costes de vehículo", "🚗", "#8B5CF6"),
    ]),
    ("Estilo de vida", "✨", "#EC4899", [
        ("Suscripciones", "🔁", "#EC4899"),
        ("Entretenimiento", "🎬", "#EC4899"),
        ("Ropa", "👕", "#EC4899"),
        ("Gimnasio", "🏋️", "#EC4899"),
        ("Bienestar", "❤️", "#EC4899"),
        ("Regalos", "🎁", "#EC4899"),
        ("Hobby", "🎨", "#EC4899"),
    ]),
    ("Salud", "🏥", "#EF4444", [
        ("Salud", "🏥", "#EF4444"),
        ("Farmacia", "💊", "#EF4444"),
    ]),
    ("Educación", "📚", "#6366F1", [
        ("Educación", "📚", "#6366F1"),
    ]),
    ("Ahorro", "🐷", "#14B8A6", [
        ("Ahorro", "🐷", "#14B8A6"),
    ]),
    ("Otros", "📦", "#94A3B8", [
        ("Impuestos", "🧾", "#94A3B8"),
        ("Comisiones", "🏛️", "#94A3B8"),
        ("Otros gastos", "💸", "#94A3B8"),
    ]),
]

DEFAULT_INCOME_CATEGORY_GROUPS = [
    ("Ingresos", "💰", "#22C55E", [
        ("Sueldo", "💼", "#22C55E"),
        ("Freelance", "🧑‍💻", "#22C55E"),
        ("Inversiones", "📈", "#22C55E"),
        ("Reembolsos", "↩️", "#22C55E"),
        ("Regalos", "🎁", "#22C55E"),
        ("Otros ingresos", "💰", "#22C55E"),
    ]),
]


def seed_default_categories(workspace) -> int:
    """
    Crea el set de categorías por defecto (grupos → categorías) en
    ``workspace`` -- idempotente (``get_or_create`` por workspace + nombre +
    tipo), así que correrlo sobre un workspace que ya tiene categorías sólo
    agrega lo que falte, nunca duplica ni pisa lo que el usuario ya armó.

    La llaman tanto ``WorkspaceViewSet.perform_create`` (todo workspace
    nuevo arranca con esto, nunca en blanco) como el comando de management
    ``seed_categories`` (para aplicarlo a mano a workspaces que ya existían
    de antes). Devuelve cuántas categorías nuevas creó.
    """
    created = 0
    order = 0
    for cat_type, groups in (
        (Category.TYPE_EXPENSE, DEFAULT_EXPENSE_CATEGORY_GROUPS),
        (Category.TYPE_INCOME, DEFAULT_INCOME_CATEGORY_GROUPS),
    ):
        for gname, gicon, gcolor, children in groups:
            group, made = Category.objects.get_or_create(
                workspace=workspace, name=gname, type=cat_type, parent=None,
                defaults={"icon": gicon, "color": gcolor, "sort_order": order},
            )
            order += 1
            created += int(made)
            for cname, cicon, ccolor in children:
                _, made = Category.objects.get_or_create(
                    workspace=workspace, name=cname, type=cat_type,
                    defaults={"icon": cicon, "color": ccolor, "parent": group, "sort_order": order},
                )
                order += 1
                created += int(made)
    # Rubro de lealtad de las categorías por defecto, para que las tarjetas con
    # recompensas funcionen desde el primer gasto de un workspace nuevo. No hace
    # nada si el catálogo de lealtad todavía no está cargado.
    from apps.loyalty.services import map_categories_to_rubros

    map_categories_to_rubros(Category.objects.filter(workspace=workspace))
    return created


# ---------------------------------------------------------------------------
# Registrar un reembolso (crea la Transaction que devuelve la plata de verdad)
# ---------------------------------------------------------------------------
def get_or_create_refund_category(workspace) -> Category:
    """
    La categoría "Reembolsos" (ingreso) que usa `register_refund`. Ya viene
    en `DEFAULT_INCOME_CATEGORY_GROUPS` -- esto sólo cubre el caso de un
    workspace de antes de que existiera esa fila en el seed, o donde el
    usuario la borró: la vuelve a crear bajo el grupo "Ingresos" (también
    get-or-create, por si ni ese existe) en vez de fallar.
    """
    existing = Category.objects.filter(
        workspace=workspace, type=Category.TYPE_INCOME, name__iexact="Reembolsos",
    ).first()
    if existing:
        return existing
    group, _ = Category.objects.get_or_create(
        workspace=workspace, name="Ingresos", type=Category.TYPE_INCOME, parent=None,
        defaults={"icon": "💰", "color": "#22C55E"},
    )
    return Category.objects.create(
        workspace=workspace, name="Reembolsos", type=Category.TYPE_INCOME, parent=group,
        icon="↩️", color="#22C55E",
    )


def register_refund(*, original: Transaction, amount: Decimal, date, wallet, created_by) -> Transaction:
    """
    Crea la Transaction de INGRESO que devuelve la plata de ``original`` (un
    gasto) y marca ``original.is_refunded = True`` -- antes de esto,
    "reembolsado" era sólo un flag sin ningún movimiento de dinero real
    detrás (ver docstring de `Transaction.is_refunded`).

    ``wallet`` ya viene resuelto por el llamador (default: la misma de
    ``original``) -- acá sólo se asume del mismo workspace, ya validado
    antes de llegar (ver `TransactionViewSet.register_refund`).
    """
    if original.type != Transaction.TYPE_EXPENSE:
        raise ValidationError({"detail": "Sólo se puede registrar un reembolso de un gasto."})
    if original.is_refunded:
        raise ValidationError({"detail": "Esta transacción ya tiene un reembolso registrado."})
    if amount <= 0:
        raise ValidationError({"amount": "Tiene que ser mayor a cero."})
    if amount > original.amount:
        raise ValidationError(
            {"amount": f"No puede ser mayor al monto original de la transacción ({original.amount})."}
        )

    category = get_or_create_refund_category(original.wallet.workspace)

    with db_transaction.atomic():
        refund = Transaction.objects.create(
            type=Transaction.TYPE_INCOME,
            wallet=wallet,
            category=category,
            amount=amount,
            description=f"Reembolso: {original.description}" if original.description else "Reembolso",
            date=date,
            created_by=created_by,
            source=Transaction.SOURCE_REFUND,
            refund_of=original,
        )
        original.is_refunded = True
        original.save(update_fields=["is_refunded", "updated_at"])

    return refund


def _advance(date, frequency):
    delta = RecurringExpense.FREQUENCY_DELTAS.get(
        frequency, RecurringExpense.FREQUENCY_DELTAS[RecurringExpense.FREQUENCY_MONTHLY]
    )
    return date + relativedelta(**delta)


def _recurring_description(rec) -> str:
    if rec.name:
        return f"{rec.name} (recurrente)"
    if rec.category:
        return f"{rec.category.name} (recurrente)"
    return f"Transferencia a {rec.to_wallet.name} (recurrente)"


def register_manual_recurring_occurrence(rec, date):
    """Avanza `next_due_date` cuando el usuario registra a mano (desde la
    tarjeta "Programado") una ocurrencia de un recurrente antes de que corra
    el job automático.

    Sin esto, `next_due_date` se queda como estaba y
    `generate_recurring_transactions` la vuelve a crear al día siguiente --
    el alta manual sólo prellenaba el formulario, nunca tocaba la regla (ver
    `openScheduledItem` en el frontend). `select_for_update` evita la
    carrera con ese mismo job si corren a la vez.
    """
    with db_transaction.atomic():
        rec = RecurringExpense.objects.select_for_update().get(pk=rec.pk)
        due = rec.next_due_date
        while due <= date:
            due = _advance(due, rec.frequency)
        if due != rec.next_due_date:
            rec.next_due_date = due
            rec.save(update_fields=["next_due_date", "updated_at"])


def generate_recurring_transactions(as_of=None):
    """Crea una Transaction por cada período vencido de cada gasto recurrente activo.

    Idempotente: avanza ``next_due_date`` a medida que genera, así una segunda
    corrida el mismo día no duplica nada. Puede generar income/expense
    (contra `category`) o transfer (contra `to_wallet`, p. ej. un aporte
    automático a una cartera de ahorro con meta) según `rec.type`.
    """
    as_of = as_of or timezone.localdate()
    created = []

    recurring = (
        RecurringExpense.objects.filter(is_active=True, next_due_date__lte=as_of)
        .select_related("category", "wallet", "to_wallet")
    )
    for rec in recurring:
        is_transfer = rec.type == RecurringExpense.TYPE_TRANSFER
        with db_transaction.atomic():
            due = rec.next_due_date
            while due <= as_of:
                created.append(
                    Transaction.objects.create(
                        type=rec.type,
                        wallet=rec.wallet,
                        to_wallet=rec.to_wallet if is_transfer else None,
                        category=None if is_transfer else rec.category,
                        amount=rec.amount,
                        description=_recurring_description(rec),
                        date=due,
                        source=Transaction.SOURCE_RECURRING,
                        is_recurring=True,
                    )
                )
                due = _advance(due, rec.frequency)
            rec.next_due_date = due
            rec.save(update_fields=["next_due_date", "updated_at"])

    return created


def installment_amounts(total_amount: Decimal, installments_total: int) -> list[Decimal]:
    """Reparte `total_amount` en `installments_total` cuotas para el cálculo
    del estado de cuenta: cada cuota, menos la última, se redondea hacia
    arriba al centavo (p. ej. USD 12.244 -> USD 12.25); la última es lo que
    sobra, para que la suma cierre exacto con `total_amount` sin importar el
    redondeo de las anteriores."""
    total_cents = int((total_amount * 100).to_integral_value(rounding=ROUND_CEILING))
    per_cents = -(-total_cents // installments_total)  # división entera hacia arriba
    amounts = [Decimal(per_cents) / 100] * (installments_total - 1)
    last = total_amount - sum(amounts, Decimal("0"))
    amounts.append(last)
    return amounts


# ---------------------------------------------------------------------------
# Detección de recurrentes ("esto se repite hace 3 meses, ¿lo marco como
# recurrente?", Fase 2 del roadmap)
# ---------------------------------------------------------------------------
def _approx_amount(amount) -> Decimal:
    """Redondea al entero más cercano -- para que una sugerencia descartada
    siga reconociéndose aunque el monto varíe unos centavos de mes a mes."""
    return Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


_MONTH_WORDS = {
    "ene", "enero", "feb", "febrero", "mar", "marzo", "abr", "abril", "may", "mayo",
    "jun", "junio", "jul", "julio", "ago", "agosto", "sep", "sept", "septiembre",
    "setiembre", "oct", "octubre", "nov", "noviembre", "dic", "diciembre",
}


def _subscription_label(txn) -> str:
    """Con qué se reconoce el mismo cobro mes a mes: el comercio si está, y
    si no la descripción sin números, signos ni nombres de mes ("NETFLIX.COM
    0923" y "Netflix.com 1023" quedan en "netflix com"). Vacío si no hay nada
    que la identifique."""
    import re

    if txn.merchant_id and txn.merchant:
        return f"m:{txn.merchant_id}"
    words = re.sub(r"[^a-záéíóúñü ]+", " ", (txn.description or "").lower()).split()
    words = [w for w in words if w not in _MONTH_WORDS and len(w) > 1]
    return " ".join(words[:4])


def _display_name(txn) -> str:
    if txn.merchant_id and txn.merchant:
        return txn.merchant.name
    return (txn.description or "").strip()


def detect_recurring_candidates(workspace, user, months=4, min_occurrences=3):
    """Transacciones que se repiten mes a mes con un monto parecido y que
    todavía no están marcadas como recurrentes: suscripciones (Netflix,
    Spotify, el gimnasio), servicios, el sueldo.

    Se agrupan dos veces:

    - Por categoría + cartera + comercio/descripción (`_subscription_label`).
      Es la que encuentra las suscripciones: dos servicios de streaming en
      la misma tarjeta y la misma categoría son dos grupos distintos.
    - Por categoría + cartera sola, como antes, para lo que no tiene una
      descripción estable (la luz cargada a mano con textos distintos cada
      mes). Sólo se usa si esa categoría+cartera no dio ya una candidata por
      la vía de arriba.

    Reglas de cada grupo, a propósito conservadoras para no inundar de falsos
    positivos una categoría con mucho movimiento como "Comida":

    - Como mucho UNA transacción por mes -- si hay más de una en algún mes,
      no es un cobro fijo, es gasto normal y se descarta el grupo entero.
    - Aparece en al menos ``min_occurrences`` de los últimos ``months`` meses.
    - El monto no varía más de 15% (o $2, lo que sea mayor) entre la
      ocurrencia más chica y la más grande.
    - No hay ya un `RecurringExpense` activo con esa categoría+cartera y un
      monto parecido.
    - El usuario no la descartó antes (`RecurringSuggestionDismissal`).
    """
    from .models import RecurringSuggestionDismissal

    until = timezone.localdate().replace(day=1)
    since = until - relativedelta(months=months - 1)

    txns = list(
        visible_transactions(workspace, user)
        .filter(date__gte=since, type__in=[Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE])
        .exclude(source__in=[Transaction.SOURCE_RECURRING, Transaction.SOURCE_INSTALLMENT])
        .exclude(category__isnull=True)
        .select_related("category", "wallet", "merchant")
        .order_by("date")
    )

    def group(key_fn):
        groups: dict = {}
        for t in txns:
            key = key_fn(t)
            if key is None:
                continue
            g = groups.setdefault(key, {"txns": [], "by_month": {}})
            g["txns"].append(t)
            g["by_month"].setdefault((t.date.year, t.date.month), []).append(t)
        return groups

    recurring_amounts = defaultdict(list)
    for rec in RecurringExpense.objects.filter(workspace=workspace, is_active=True):
        recurring_amounts[(rec.category_id, rec.wallet_id)].append(rec.amount)
    dismissed = {
        (d.category_id, d.wallet_id, d.approx_amount)
        for d in RecurringSuggestionDismissal.objects.filter(workspace=workspace)
    }

    def candidate(g):
        by_month = g["by_month"]
        if any(len(v) > 1 for v in by_month.values()):
            return None
        if len(by_month) < min_occurrences:
            return None
        amounts = [v[0].amount for v in by_month.values()]
        avg = sum(amounts) / len(amounts)
        tolerance = max(Decimal("2"), avg * Decimal("0.15"))
        if max(amounts) - min(amounts) > tolerance:
            return None
        last_txn = max((v[0] for v in by_month.values()), key=lambda t: t.date)
        cat_wallet = (last_txn.category_id, last_txn.wallet_id)
        if any(abs(a - avg) <= tolerance for a in recurring_amounts[cat_wallet]):
            return None
        if (*cat_wallet, _approx_amount(avg)) in dismissed:
            return None
        return {
            "type": last_txn.type,
            "category": last_txn.category_id,
            "category_name": last_txn.category.name,
            "wallet": last_txn.wallet_id,
            "wallet_name": last_txn.wallet.name,
            "name": _display_name(last_txn),
            "suggested_amount": avg.quantize(Decimal("0.01")),
            "occurrences": len(by_month),
            "last_date": last_txn.date,
            "suggested_next_due_date": last_txn.date + relativedelta(months=1),
        }

    candidates = []
    covered = set()
    by_label = group(
        lambda t: (t.type, t.category_id, t.wallet_id, label)
        if (label := _subscription_label(t))
        else None
    )
    for g in by_label.values():
        c = candidate(g)
        if c:
            candidates.append(c)
            covered.add((c["type"], c["category"], c["wallet"]))
    for key, g in group(lambda t: (t.type, t.category_id, t.wallet_id)).items():
        if key in covered:
            continue
        c = candidate(g)
        if c:
            # Sin descripción estable, el nombre de una ocurrencia suelta
            # confunde más de lo que ayuda: se nombra por la categoría.
            c["name"] = ""
            candidates.append(c)

    candidates.sort(key=lambda c: -c["occurrences"])
    return candidates


def dismiss_recurring_suggestion(workspace, category, wallet, amount):
    from .models import RecurringSuggestionDismissal

    RecurringSuggestionDismissal.objects.get_or_create(
        workspace=workspace, category=category, wallet=wallet,
        approx_amount=_approx_amount(amount),
    )


# ---------------------------------------------------------------------------
# Duplicados (Apple Pay / correo / carga manual)
# ---------------------------------------------------------------------------
# Ventana de tolerancia: algunos bancos reportan la fecha del cargo con un
# día de diferencia respecto a cuándo ocurrió de verdad, y una notificación
# de Apple Pay o un correo bancario reenviado puede volver a dispararse un
# rato después (mismo día casi siempre, pero no siempre).
DUPLICATE_WINDOW_DAYS = 1


def find_possible_duplicates(*, wallet, amount, date, exclude_id=None):
    """Transacciones vivas en `wallet` con el mismo monto y una fecha dentro
    de ±`DUPLICATE_WINDOW_DAYS` días -- candidatas a duplicado. No filtra por
    `source`: un alta manual el mismo día que ya importó el correo del banco
    también cuenta."""
    start = date - dt.timedelta(days=DUPLICATE_WINDOW_DAYS)
    end = date + dt.timedelta(days=DUPLICATE_WINDOW_DAYS)
    qs = Transaction.objects.filter(wallet=wallet, amount=amount, date__gte=start, date__lte=end)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.order_by("-date")


# ---------------------------------------------------------------------------
# División de transacciones entre personas
# ---------------------------------------------------------------------------
def get_or_create_self_person(workspace, membership):
    """El `Person` que representa al usuario autenticado dentro de un split
    -- se crea la primera vez que hace falta (al pagar o participar en una
    división), no al crear el workspace."""
    person, created = Person.objects.get_or_create(
        workspace=workspace,
        member=membership,
        defaults={"name": membership.user.get_full_name() or membership.user.email},
    )
    return person


def person_balances(workspace):
    """Balance neto entre cada par de personas del workspace que tiene
    divisiones sin liquidar: quién le debe cuánto a quién, ya neteado (si A
    le debe 10 a B y B le debe 4 a A, el resultado es un solo renglón: A le
    debe 6 a B). No incluye pares en 0."""
    shares = (
        TransactionShare.objects.filter(
            transaction__wallet__workspace=workspace,
            transaction__is_deleted=False,
            is_deleted=False,
            is_settled=False,
            transaction__paid_by__isnull=False,
        )
        .exclude(person=F("transaction__paid_by"))
        .select_related("person", "transaction__paid_by")
    )

    owed = defaultdict(Decimal)  # (debtor_id, creditor_id) -> monto
    people_by_id = {}
    for share in shares:
        debtor = share.person
        creditor = share.transaction.paid_by
        people_by_id[debtor.id] = debtor
        people_by_id[creditor.id] = creditor
        owed[(debtor.id, creditor.id)] += share.amount

    seen = set()
    results = []
    for (debtor_id, creditor_id), amount in owed.items():
        pair = frozenset((debtor_id, creditor_id))
        if pair in seen:
            continue
        seen.add(pair)
        reverse_amount = owed.get((creditor_id, debtor_id), Decimal("0"))
        net = amount - reverse_amount
        if net > 0:
            results.append(
                {"from_person": people_by_id[debtor_id], "to_person": people_by_id[creditor_id], "amount": net}
            )
        elif net < 0:
            results.append(
                {"from_person": people_by_id[creditor_id], "to_person": people_by_id[debtor_id], "amount": -net}
            )
    results.sort(key=lambda r: -r["amount"])
    return results


def settle_balance(workspace, person_a, person_b):
    """Marca como liquidadas TODAS las partes sin liquidar entre `person_a`
    y `person_b`, en cualquier dirección -- equivale a "saldar" la fila neta
    que devuelve `person_balances` para ese par (que puede estar compuesta
    por varias `TransactionShare` de distintas transacciones). Devuelve
    cuántas se actualizaron."""
    return (
        TransactionShare.objects.filter(
            transaction__wallet__workspace=workspace,
            transaction__is_deleted=False,
            is_deleted=False,
            is_settled=False,
        )
        .filter(
            Q(person=person_a, transaction__paid_by=person_b)
            | Q(person=person_b, transaction__paid_by=person_a)
        )
        .update(is_settled=True, settled_at=timezone.now())
    )
