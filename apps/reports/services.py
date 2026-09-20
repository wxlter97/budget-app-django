"""Cierre de mes: snapshot financiero + rollover de provisiones.

Lo dispara la tarea de Celery Beat el día 1 (para el mes anterior), pero se
puede llamar a mano para cualquier (año, mes).
"""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import F, Q
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.accounts.services import credit_card_statement, installment_status
from apps.common import periods
from apps.transactions.models import (
    Category,
    CategoryBudget,
    CategoryProvision,
    InstallmentPurchase,
    RecurringExpense,
    Transaction,
)
from apps.transactions.services import _advance, visible_transactions
from apps.workspaces.currency import convert, get_rate_map
from apps.workspaces.models import Workspace

from .models import MonthlySnapshot


def _sum_converted(qs, rate_map, amount_field="amount", currency_field="currency"):
    """Como `_sum`, pero convirtiendo cada fila a la moneda base antes de
    sumar -- `Sum()` de la base de datos no puede aplicar una tasa distinta
    por fila. Las filas en una moneda sin tasa configurada simplemente no
    se cuentan (ver `apps.workspaces.currency.convert`)."""
    total = Decimal("0")
    for row in qs.values(amount_field, currency_field):
        converted = convert(row[amount_field], row[currency_field], rate_map)
        if converted is not None:
            total += converted
    return total


def _visible(qs, user):
    if user is None:
        return qs
    return qs.filter(Q(visibility=Wallet.VISIBILITY_SHARED) | Q(owner=user))


def net_worth_breakdown(workspace, user=None, rate_map=None) -> dict:
    """
    Patrimonio neto + totales por tipo de cartera, convertidos a
    ``workspace.base_currency`` (ver ``apps.workspaces.currency``) -- una
    cartera en una moneda sin tasa configurada no entra en ningún total.

    - ``net``       = Σ ``current_balance`` (propio, sin hijos, para no doble-contar)
                      de las carteras activas con ``counts_toward_net_worth=True``.
    - ``by_purpose``= Σ ``current_balance`` por ``purpose``, ignorando el flag.

    Con ``user`` se excluyen las carteras privadas de las que no es owner.
    """
    # Incluye las archivadas: siguen sumando al patrimonio neto (como en Buddy).
    wallets = list(
        _visible(Wallet.objects.filter(workspace=workspace), user)
    )
    # `rate_map` lo pasa quien ya lo tiene (ver `dashboard_summary`): son consultas de más.
    rate_map = rate_map if rate_map is not None else get_rate_map(workspace)
    converted = [
        (w, convert(w.current_balance, w.currency, rate_map)) for w in wallets
    ]

    net = sum(
        (c for w, c in converted if w.counts_toward_net_worth and c is not None),
        Decimal("0"),
    )
    by_purpose = {
        purpose: sum(
            (c for w, c in converted if w.purpose == purpose and c is not None),
            Decimal("0"),
        )
        for purpose, _ in Wallet.PURPOSE_CHOICES
    }
    return {"net": net, "by_purpose": by_purpose, "base_currency": workspace.base_currency}


def net_worth(workspace) -> Decimal:
    return net_worth_breakdown(workspace)["net"]


# Dinero que "sale" y se clasifica: gastos + transferencias con categoría
# (p. ej. mover a ahorro). No cuenta ingresos ni transferencias sin categoría.
_OUTFLOW_Q = Q(type=Transaction.TYPE_EXPENSE) | Q(
    type=Transaction.TYPE_TRANSFER, category__isnull=False
)


def spending_by_category(workspace, user, year, month, rate_map=None):
    rate_map = rate_map if rate_map is not None else get_rate_map(workspace)
    rows = (
        visible_transactions(workspace, user)
        .filter(date__year=year, date__month=month)
        .filter(_OUTFLOW_Q, category__isnull=False)
        .values("category_id", "category__name", "amount", "currency")
    )

    totals: dict[str, dict] = {}
    order = []
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None:
            continue
        key = str(r["category_id"])
        if key not in totals:
            totals[key] = {
                "category": key,
                "category_name": r["category__name"],
                "spent": Decimal("0"),
            }
            order.append(key)
        totals[key]["spent"] += converted

    result = [totals[k] for k in order]
    result.sort(key=lambda r: -r["spent"])
    return result


def budget_vs_actual(workspace, user, period_start):
    """Presupuesto vs. gasto real por categoría para el período (ver
    `apps.common.periods`) que arranca en `period_start`, con la cadencia
    de `workspace.budget_period`.

    ``budgeted`` se asume siempre en ``workspace.base_currency`` (el monto
    del presupuesto no está atado a ninguna cartera); ``spent`` se convierte
    desde la moneda de cada transacción -- ver ``apps.workspaces.currency``.
    """
    period_end = periods.period_end(period_start, workspace.budget_period)
    rate_map = get_rate_map(workspace)
    # Un grupo CON subcategorías no debería tener presupuesto propio aparte
    # (ver `CategoryBudgetSerializer.validate_category`, que ya lo impide
    # hacia adelante) -- este filtro sanea cualquier resto que haya quedado
    # de antes de esa validación, que si no duplicaría el total del grupo
    # con el de sus subcategorías. Un grupo SIN subcategorías sigue siendo
    # presupuestable directamente (es su propia unidad).
    groups_with_children = set(
        Category.objects.filter(
            workspace=workspace, parent__isnull=False, is_deleted=False
        ).values_list("parent_id", flat=True)
    )
    budgets = {
        b.category_id: b.amount
        for b in CategoryBudget.objects.filter(workspace=workspace, period_start=period_start)
        if b.category_id not in groups_with_children
    }
    spent: dict = {}
    for row in (
        visible_transactions(workspace, user)
        .filter(date__gte=period_start, date__lte=period_end, counts_toward_budget=True)
        .filter(_OUTFLOW_Q)
        .values("category", "amount", "currency")
    ):
        converted = convert(row["amount"], row["currency"], rate_map)
        if converted is None:
            continue
        spent[row["category"]] = spent.get(row["category"], Decimal("0")) + converted
    provisions = {
        p.category_id: p.accumulated_amount
        for p in CategoryProvision.objects.filter(category__workspace=workspace)
    }

    cat_ids = set(budgets) | set(spent)
    cats = {
        str(c.id): c
        for c in Category.objects.filter(id__in=cat_ids).select_related("parent")
    }

    rows = []
    for cid in cat_ids:
        budgeted = budgets.get(cid, Decimal("0"))
        used = spent.get(cid, Decimal("0"))
        cat = cats.get(str(cid))
        rows.append(
            {
                "category": str(cid),
                "category_name": cat.name if cat else None,
                "budgeted": budgeted,
                "spent": used,
                "remaining": budgeted - used,
                "provision": provisions.get(cid, Decimal("0")),
            }
        )
    rows.sort(key=lambda r: (r["category_name"] or "").lower())

    totals = {
        "budgeted": sum((r["budgeted"] for r in rows), Decimal("0")),
        "spent": sum((r["spent"] for r in rows), Decimal("0")),
        "remaining": sum((r["remaining"] for r in rows), Decimal("0")),
    }
    groups = _group_budget_rows(rows, cats)
    return {
        "period_start": period_start,
        "period_end": period_end,
        "base_currency": workspace.base_currency,
        "rows": rows,
        "groups": groups,
        "totals": totals,
    }


def _group_budget_rows(rows, cats):
    """Agrupa las filas de presupuesto por su grupo (categoría padre).

    Una categoría sin padre es su propio grupo. El resultado alimenta el
    anillo "restante para gastar" y las tarjetas por grupo del cliente.
    """
    buckets = {}
    order = []
    for row in rows:
        cat = cats.get(row["category"])
        if cat is not None and cat.parent_id is not None:
            gid, gname = cat.parent_id, (cat.parent.name if cat.parent else None)
        elif cat is not None:
            gid, gname = cat.id, cat.name
        else:
            gid, gname = None, None
        key = str(gid) if gid else "__none__"
        if key not in buckets:
            buckets[key] = {
                "group": str(gid) if gid else None,
                "group_name": gname or "Sin grupo",
                "budgeted": Decimal("0"),
                "spent": Decimal("0"),
                "remaining": Decimal("0"),
                "rows": [],
            }
            order.append(key)
        b = buckets[key]
        b["rows"].append(row)
        b["budgeted"] += row["budgeted"]
        b["spent"] += row["spent"]
        b["remaining"] += row["remaining"]

    result = [buckets[k] for k in order]
    result.sort(key=lambda g: (g["group_name"] or "").lower())
    return result


def monthly_cashflow(workspace, user, months=6, until=None, rate_map=None):
    until = (until or timezone.localdate()).replace(day=1)
    rate_map = rate_map if rate_map is not None else get_rate_map(workspace)
    periods = []
    cursor = until
    for _ in range(months):
        periods.append((cursor.year, cursor.month))
        cursor -= relativedelta(months=1)
    periods.reverse()

    txns = visible_transactions(workspace, user)
    series = []
    for year, month in periods:
        month_txns = txns.filter(date__year=year, date__month=month)
        income = _sum_converted(month_txns.filter(type=Transaction.TYPE_INCOME), rate_map)
        expenses = _sum_converted(month_txns.filter(type=Transaction.TYPE_EXPENSE), rate_map)
        series.append(
            {
                "year": year,
                "month": month,
                "income": income,
                "expenses": expenses,
                "net": income - expenses,
            }
        )
    return series


def category_trends(workspace, user, months=6):
    """Gasto mensual por categoría de los últimos `months` meses + cuáles
    crecieron (o bajaron) más entre el mes en curso y el anterior.

    Reusa `spending_by_category` mes a mes -- son pocos meses (máx. 24) y el
    workspace de una app personal no tiene tantas categorías, así que
    N queries chicas es más simple que armar una sola con `TruncMonth`.
    """
    until = timezone.localdate().replace(day=1)
    periods = []
    cursor = until
    for _ in range(months):
        periods.append((cursor.year, cursor.month))
        cursor -= relativedelta(months=1)
    periods.reverse()

    by_cat: dict[str, dict] = {}
    for idx, (year, month) in enumerate(periods):
        for row in spending_by_category(workspace, user, year, month):
            key = row["category"]
            if key not in by_cat:
                by_cat[key] = {
                    "category": key,
                    "category_name": row["category_name"],
                    "amounts": [Decimal("0")] * months,
                }
            by_cat[key]["amounts"][idx] = row["spent"]

    categories = list(by_cat.values())
    for c in categories:
        last, prev = c["amounts"][-1], c["amounts"][-2] if months > 1 else Decimal("0")
        c["change"] = last - prev
        c["change_pct"] = float(c["change"] / prev * 100) if prev else None

    # El que más creció primero -- lo que interesa mostrar es "esto se te
    # disparó", no un orden alfabético ni por total.
    categories.sort(key=lambda c: c["change"], reverse=True)

    return {
        "months": [{"year": y, "month": m} for y, m in periods],
        "categories": categories,
    }


def dashboard_summary(workspace, user, today=None):
    today = today or timezone.localdate()
    from apps.email_import.models import EmailImportLog

    # Una sola lectura de las tasas para las tres cuentas de abajo.
    rate_map = get_rate_map(workspace)
    this_month = monthly_cashflow(workspace, user, months=1, until=today, rate_map=rate_map)[0]
    return {
        "month": this_month,
        "net_worth": net_worth_breakdown(workspace, user, rate_map=rate_map)["net"],
        "base_currency": workspace.base_currency,
        "pending_email_imports": EmailImportLog.objects.filter(
            workspace=workspace, status=EmailImportLog.STATUS_PENDING
        ).count(),
        "top_expense_categories": spending_by_category(
            workspace, user, today.year, today.month, rate_map=rate_map
        )[:5],
    }


def upcoming_scheduled(workspace, user, until=None, since=None):
    """Ocurrencias futuras de gastos recurrentes + cuotas + vencimientos de
    tarjeta/deuda, SIN crear nada.

    Alimenta la tarjeta "PROGRAMADO", los marcadores de la lista y el
    calendario financiero. `since` por defecto hoy, `until` por defecto fin
    del mes en curso -- ambos aceptan cualquier rango (el calendario los usa
    con el mes que se esté mirando, no necesariamente el actual).
    """
    today = timezone.localdate()
    since = since or today
    if until is None:
        until = (today.replace(day=1) + relativedelta(months=1)) - relativedelta(days=1)

    def _wallet_ok(w):
        return w.visibility == Wallet.VISIBILITY_SHARED or w.owner_id == getattr(
            user, "id", None
        )

    items = []

    recurring = RecurringExpense.objects.filter(
        workspace=workspace, is_active=True, next_due_date__lte=until
    ).select_related("category", "wallet", "to_wallet")
    for rec in recurring:
        if not _wallet_ok(rec.wallet):
            continue
        is_transfer = rec.type == RecurringExpense.TYPE_TRANSFER
        due = rec.next_due_date
        guard = 0
        while due <= until and guard < 400:
            guard += 1
            if due >= since:
                items.append(
                    {
                        "date": due,
                        "kind": "recurring",
                        # Un recurrente puede ser income/expense/transfer
                        # (ver `RecurringExpense.type`) -- sin esto, un
                        # sueldo recurrente se mostraba en "Programado" como
                        # si fuera un gasto más (signo y color de salida).
                        "type": rec.type,
                        "source_id": rec.id,
                        "description": (
                            f"Transferencia a {rec.to_wallet.name}" if is_transfer
                            else rec.category.name
                        ),
                        "amount": rec.amount,
                        "category": None if is_transfer else rec.category_id,
                        "category_name": None if is_transfer else rec.category.name,
                        "wallet": rec.wallet_id,
                        "wallet_name": rec.wallet.name,
                        "to_wallet": rec.to_wallet_id if is_transfer else None,
                        "to_wallet_name": rec.to_wallet.name if is_transfer else None,
                    }
                )
            due = _advance(due, rec.frequency)

    installments = InstallmentPurchase.objects.filter(
        workspace=workspace
    ).select_related("category", "wallet")
    for pur in installments:
        if not _wallet_ok(pur.wallet):
            continue
        # Las cuotas ya no llevan contador propio -- se calculan sobre los
        # cortes de la tarjeta (ver `installment_status`); acá solo se listan
        # las que todavía no vencieron, como recordatorio de lo que se viene.
        status = installment_status(pur)
        for line in status["schedule"][status["installments_paid"] :]:
            due = line["cutoff_date"]
            if due < since or due > until:
                continue
            items.append(
                {
                    "date": due,
                    "kind": "installment",
                    "type": "expense",
                    "source_id": pur.id,
                    "description": f"{pur.description} (cuota {line['n']}/{pur.installments_total})",
                    "amount": line["amount"],
                    "category": pur.category_id,
                    "category_name": pur.category.name,
                    "wallet": pur.wallet_id,
                    "wallet_name": pur.wallet.name,
                    "to_wallet": None,
                    "to_wallet_name": None,
                }
            )

    # Pago de tarjeta de crédito: sólo el vencimiento del ciclo YA cortado
    # (o por cortar), nunca de ciclos futuros -- de esos no se puede saber
    # el monto todavía (depende de gasto que no pasó). Un solo ítem por
    # tarjeta, no una proyección mensual como los recurrentes.
    cards = (
        Wallet.objects.filter(
            workspace=workspace, kind=Wallet.KIND_CREDIT, is_archived=False
        )
        .exclude(billing_cycle_day__isnull=True)
    )
    for card in cards:
        if not _wallet_ok(card):
            continue
        statement = credit_card_statement(card, as_of=today)
        due = statement["payment_due_date"] if statement else None
        if due is None or due < since or due > until:
            continue
        items.append(
            {
                "date": due,
                "kind": "card_payment",
                "type": "expense",
                "source_id": card.id,
                "description": f"Pago de tarjeta · {card.name}",
                "amount": statement["total_due"],
                "category": None,
                "category_name": None,
                "wallet": card.id,
                "wallet_name": card.name,
                "to_wallet": None,
                "to_wallet_name": None,
            }
        )

    # Deudas con fecha de vencimiento fija (préstamos, no tarjetas -- esas ya
    # se cubren arriba por su propio ciclo). Un solo vencimiento, no
    # recurrente: `Wallet.due_date` es una fecha puntual, no un día del mes.
    debts = Wallet.objects.filter(
        workspace=workspace,
        purpose=Wallet.PURPOSE_DEBT,
        is_archived=False,
        due_date__isnull=False,
    )
    for debt in debts:
        if not _wallet_ok(debt) or debt.due_date < since or debt.due_date > until:
            continue
        items.append(
            {
                "date": debt.due_date,
                "kind": "debt_due",
                "type": "expense",
                "source_id": debt.id,
                "description": f"Vencimiento · {debt.name}",
                "amount": abs(debt.current_balance),
                "category": None,
                "category_name": None,
                "wallet": debt.id,
                "wallet_name": debt.name,
                "to_wallet": None,
                "to_wallet_name": None,
            }
        )

    items.sort(key=lambda i: i["date"])
    return items


def close_month(year, month, workspace=None):
    """Crea/actualiza el MonthlySnapshot de cada workspace (histórico de
    patrimonio neto, ver NetWorthHistoryScreen -- siempre mensual sin
    importar `workspace.budget_period`, es un concepto aparte del
    presupuesto). El rollover de provisión de presupuesto va por separado en
    `close_previous_budget_period`, con su propia cadencia."""
    workspaces = [workspace] if workspace is not None else Workspace.objects.all()
    snapshots = []

    for ws in workspaces:
        rate_map = get_rate_map(ws)
        month_txns = Transaction.objects.filter(
            wallet__workspace=ws, date__year=year, date__month=month
        )
        snapshot, _ = MonthlySnapshot.objects.update_or_create(
            workspace=ws,
            year=year,
            month=month,
            defaults={
                "total_net_worth": net_worth(ws),
                "total_income": _sum_converted(
                    month_txns.filter(type=Transaction.TYPE_INCOME), rate_map
                ),
                "total_expenses": _sum_converted(
                    month_txns.filter(type=Transaction.TYPE_EXPENSE), rate_map
                ),
            },
        )
        snapshots.append(snapshot)

    return snapshots


# Tope de períodos atrasados que se ponen al día en una sola corrida (ver
# `close_previous_budget_period`) -- cubre de sobra el peor caso realista
# (cron caído varios días con `budget_period=daily`) sin arriesgar un bucle
# larguísimo si `budget_period_closed_through` quedara desalineado.
_MAX_CATCHUP_PERIODS = 400


def close_previous_budget_period(workspace=None, as_of=None):
    """Le hace rollover de provisión al último período de presupuesto ya
    terminado de cada workspace (ver `apps.common.periods`), con la cadencia
    de `workspace.budget_period`. Se guarda hasta dónde se cerró en
    `workspace.budget_period_closed_through` para no volver a sumar el mismo
    sobrante dos veces si esto corre más de una vez el mismo período (a
    diferencia de `close_month`/`MonthlySnapshot`, que sólo sobreescribe, acá
    el acumulado de `CategoryProvision` se incrementa -- correrlo de más sin
    esta guarda lo iría duplicando).

    `as_of` (default: hoy) es "desde qué fecha" se mira para decidir cuál es
    el último período ya terminado -- parametrizable para poder probar esto
    con fechas fijas en vez de la fecha real del sistema.
    """
    workspaces = [workspace] if workspace is not None else Workspace.objects.all()
    today = as_of if as_of is not None else timezone.localdate()
    closed = []

    for ws in workspaces:
        current_start = periods.period_start(today, ws.budget_period)
        last_ended = periods.previous_period_start(current_start, ws.budget_period)

        if ws.budget_period_closed_through is not None:
            cursor = periods.next_period_start(ws.budget_period_closed_through, ws.budget_period)
        else:
            cursor = last_ended  # primera corrida: no reconstruye historial previo

        n = 0
        while cursor <= last_ended and n < _MAX_CATCHUP_PERIODS:
            _rollover_budget_period(ws, cursor)
            cursor = periods.next_period_start(cursor, ws.budget_period)
            n += 1

        if n > 0:
            ws.budget_period_closed_through = last_ended
            ws.save(update_fields=["budget_period_closed_through", "updated_at"])
            closed.append(str(ws.id))

    return closed


def _rollover_budget_period(workspace, period_start):
    """Suma el sobrante (presupuesto - gasto real) de cada categoría, para
    ese período, a su provisión acumulada."""
    period_end = periods.period_end(period_start, workspace.budget_period)
    rate_map = get_rate_map(workspace)
    budgets = CategoryBudget.objects.filter(
        workspace=workspace, period_start=period_start
    ).select_related("category")

    for budget in budgets:
        spent = _sum_converted(
            Transaction.objects.filter(
                category=budget.category,
                date__gte=period_start,
                date__lte=period_end,
                counts_toward_budget=True,
            ),
            rate_map,
        )
        leftover = budget.amount - spent
        if leftover <= 0:
            continue

        provision, _ = CategoryProvision.objects.get_or_create(category=budget.category)
        CategoryProvision.objects.filter(pk=provision.pk).update(
            accumulated_amount=F("accumulated_amount") + leftover,
            last_updated=timezone.localdate(),
        )


# ---------------------------------------------------------------------------
# Insights de comportamiento ("behavioral finance"): patrones de gasto, no
# números fríos -- los convierte en notificaciones
# `apps.notifications.services.notify_insights` (llamada diaria, ver
# `NotificationPreference.warn_insights`). Nada de datos nuevos: todo sale
# de `Transaction` (fecha, monto, categoría, tipo).
#
# Cada detector devuelve `None` si no aplica (poca data, o el patrón no cruza
# el umbral) o un dict {dedupe_key, title, body}. Los que miden un patrón
# EN CURSO (fin de semana, día pico, post-cobro) dedupean por semana ISO --
# `notify_insights` corre todos los días, pero `NotificationLog` sólo deja
# pasar un aviso por semana por patrón. Los que miden un TOTAL/COMPARACIÓN
# del mes (hormiga, categoría/frecuencia disparada) dedupean por mes, porque
# repetir el mismo aviso cada semana sobre el mismo mes sería ruido.
INSIGHTS_MIN_HISTORY_DAYS = 30

_WEEKEND_LOOKBACK_DAYS = 60
_WEEKEND_DIFF_PCT = 20
_WEEKEND_MIN_WEEKEND_DAYS = 6
_WEEKEND_MIN_WEEKDAY_DAYS = 10

_POST_INCOME_LOOKBACK_DAYS = 90
_POST_INCOME_WINDOW_DAYS = 3
_POST_INCOME_DIFF_PCT = 20
_POST_INCOME_MIN_EVENTS = 2

_SMALL_PURCHASE_THRESHOLD = Decimal("15")
_SMALL_PURCHASE_MIN_TOTAL = Decimal("50")
_SMALL_PURCHASE_MIN_COUNT = 5

_PEAK_DAY_LOOKBACK_DAYS = 60
_PEAK_DAY_DIFF_PCT = 50
_PEAK_DAY_MIN_CALENDAR_DAYS = 4

_SPIKE_LOOKBACK_MONTHS = 3
_CATEGORY_SPIKE_DIFF_PCT = 40
_CATEGORY_SPIKE_MIN_AMOUNT = Decimal("20")
_FREQUENCY_SPIKE_DIFF_PCT = 50
_FREQUENCY_SPIKE_MIN_COUNT = 4

_WEEKDAY_NAMES = [
    "los lunes", "los martes", "los miércoles", "los jueves",
    "los viernes", "los sábados", "los domingos",
]


def _insight_weekend(workspace, qs, rate_map, today, first_date, week_label):
    """"Gastás más los fines de semana" -- promedio de gasto por día
    calendario (sábado/domingo) vs. por día calendario (lunes a viernes) en
    la ventana, contando también los días sin ningún gasto."""
    since = max(today - dt.timedelta(days=_WEEKEND_LOOKBACK_DAYS), first_date)
    rows = qs.filter(_OUTFLOW_Q, date__gte=since, date__lte=today).values(
        "date", "amount", "currency"
    )
    weekend_total = weekday_total = Decimal("0")
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None:
            continue
        if r["date"].weekday() >= 5:
            weekend_total += converted
        else:
            weekday_total += converted

    weekend_days = weekday_days = 0
    d = since
    while d <= today:
        if d.weekday() >= 5:
            weekend_days += 1
        else:
            weekday_days += 1
        d += dt.timedelta(days=1)
    if weekend_days < _WEEKEND_MIN_WEEKEND_DAYS or weekday_days < _WEEKEND_MIN_WEEKDAY_DAYS:
        return None

    weekday_avg = weekday_total / weekday_days
    if weekday_avg <= 0:
        return None
    weekend_avg = weekend_total / weekend_days
    diff_pct = (weekend_avg - weekday_avg) / weekday_avg * 100
    if diff_pct < _WEEKEND_DIFF_PCT:
        return None

    return {
        "dedupe_key": f"{workspace.id}:weekend:{week_label}",
        "title": "Gastás más los fines de semana",
        "body": f"En promedio gastás {diff_pct:.0f}% más por día los sábados y domingos que entre semana.",
    }


def _insight_post_income(workspace, qs, rate_map, today, first_date, week_label):
    """"Gastás más después de cobrar" -- promedio de gasto en los días
    siguientes a un ingreso (`Transaction.TYPE_INCOME`) vs. el resto de la
    ventana."""
    since = max(today - dt.timedelta(days=_POST_INCOME_LOOKBACK_DAYS), first_date)
    income_dates = set(
        qs.filter(type=Transaction.TYPE_INCOME, date__gte=since, date__lte=today).values_list(
            "date", flat=True
        )
    )
    if len(income_dates) < _POST_INCOME_MIN_EVENTS:
        return None

    post_income_dates = set()
    for income_date in income_dates:
        for offset in range(1, _POST_INCOME_WINDOW_DAYS + 1):
            d = income_date + dt.timedelta(days=offset)
            if since <= d <= today:
                post_income_dates.add(d)
    if not post_income_dates:
        return None

    rows = qs.filter(_OUTFLOW_Q, date__gte=since, date__lte=today).values(
        "date", "amount", "currency"
    )
    post_total = other_total = Decimal("0")
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None:
            continue
        if r["date"] in post_income_dates:
            post_total += converted
        else:
            other_total += converted

    total_days = (today - since).days + 1
    other_days = total_days - len(post_income_dates)
    if other_days <= 0:
        return None
    other_avg = other_total / other_days
    if other_avg <= 0:
        return None
    post_avg = post_total / len(post_income_dates)
    diff_pct = (post_avg - other_avg) / other_avg * 100
    if diff_pct < _POST_INCOME_DIFF_PCT:
        return None

    return {
        "dedupe_key": f"{workspace.id}:post_income:{week_label}",
        "title": "Gastás más justo después de cobrar",
        "body": (
            f"En los {_POST_INCOME_WINDOW_DAYS} días después de un ingreso gastás en "
            f"promedio {diff_pct:.0f}% más por día que el resto del tiempo."
        ),
    }


def _insight_small_purchases(workspace, qs, rate_map, today, first_date, week_label):
    """"Gasto hormiga" -- cuánto suman este mes las compras chicas
    (menores a `_SMALL_PURCHASE_THRESHOLD`, en la moneda base)."""
    month_start = today.replace(day=1)
    rows = qs.filter(_OUTFLOW_Q, date__gte=month_start, date__lte=today).values(
        "amount", "currency"
    )
    total = Decimal("0")
    count = 0
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None or converted >= _SMALL_PURCHASE_THRESHOLD:
            continue
        total += converted
        count += 1
    if count < _SMALL_PURCHASE_MIN_COUNT or total < _SMALL_PURCHASE_MIN_TOTAL:
        return None

    return {
        "dedupe_key": f"{workspace.id}:small_purchases:{today.year}-{today.month:02d}",
        "title": "Tus compras chicas suman más de lo que parece",
        "body": (
            f"Ya llevás {count} compras de menos de {_SMALL_PURCHASE_THRESHOLD} "
            f"{workspace.base_currency} este mes, que suman {total:.2f} {workspace.base_currency}."
        ),
    }


def _insight_peak_day(workspace, qs, rate_map, today, first_date, week_label):
    """"Tenés un día pico" -- el día de la semana con mayor gasto promedio
    por día calendario, comparado contra el promedio del resto de los
    días."""
    since = max(today - dt.timedelta(days=_PEAK_DAY_LOOKBACK_DAYS), first_date)
    rows = qs.filter(_OUTFLOW_Q, date__gte=since, date__lte=today).values(
        "date", "amount", "currency"
    )
    totals_by_weekday = defaultdict(Decimal)
    for r in rows:
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is None:
            continue
        totals_by_weekday[r["date"].weekday()] += converted

    day_counts = defaultdict(int)
    d = since
    while d <= today:
        day_counts[d.weekday()] += 1
        d += dt.timedelta(days=1)

    averages = {
        wd: totals_by_weekday[wd] / day_counts[wd]
        for wd in range(7)
        if day_counts[wd] >= _PEAK_DAY_MIN_CALENDAR_DAYS
    }
    if len(averages) < 5:
        return None

    peak_wd = max(averages, key=averages.get)
    others = [avg for wd, avg in averages.items() if wd != peak_wd]
    others_avg = sum(others) / len(others)
    if others_avg <= 0:
        return None
    diff_pct = (averages[peak_wd] - others_avg) / others_avg * 100
    if diff_pct < _PEAK_DAY_DIFF_PCT:
        return None

    return {
        "dedupe_key": f"{workspace.id}:peak_day:{week_label}",
        "title": "Tenés un día de la semana pico",
        "body": f"{_WEEKDAY_NAMES[peak_wd]} gastás en promedio {diff_pct:.0f}% más que el resto de los días.",
    }


def _category_month_stats(qs, rate_map, today):
    """Totales y conteos por categoría del mes en curso, y de la línea de
    base (promedio de los `_SPIKE_LOOKBACK_MONTHS` meses anteriores
    completos) -- compartido por `_insight_category_spike` y
    `_insight_frequency_spike` para no repetir las mismas queries."""
    month_start = today.replace(day=1)
    days_elapsed = (today - month_start).days + 1

    current_totals = defaultdict(Decimal)
    current_counts = defaultdict(int)
    names = {}
    rows = qs.filter(
        _OUTFLOW_Q, category__isnull=False, date__gte=month_start, date__lte=today
    ).values("category_id", "category__name", "amount", "currency")
    for r in rows:
        cat_id = r["category_id"]
        names[cat_id] = r["category__name"]
        current_counts[cat_id] += 1
        converted = convert(r["amount"], r["currency"], rate_map)
        if converted is not None:
            current_totals[cat_id] += converted

    baseline_totals = defaultdict(Decimal)
    baseline_counts = defaultdict(int)
    baseline_days = 0
    cursor = month_start
    for _ in range(_SPIKE_LOOKBACK_MONTHS):
        prev_start = cursor - relativedelta(months=1)
        prev_end = cursor - dt.timedelta(days=1)
        baseline_days += (prev_end - prev_start).days + 1
        rows = qs.filter(
            _OUTFLOW_Q, category__isnull=False, date__gte=prev_start, date__lte=prev_end
        ).values("category_id", "amount", "currency")
        for r in rows:
            baseline_counts[r["category_id"]] += 1
            converted = convert(r["amount"], r["currency"], rate_map)
            if converted is not None:
                baseline_totals[r["category_id"]] += converted
        cursor = prev_start

    return {
        "days_elapsed": days_elapsed,
        "baseline_days": baseline_days,
        "current_totals": current_totals,
        "current_counts": current_counts,
        "baseline_totals": baseline_totals,
        "baseline_counts": baseline_counts,
        "names": names,
    }


def _insight_category_spike(workspace, today, stats):
    """"Categoría en alza" -- el ritmo diario de gasto de una categoría este
    mes vs. su ritmo diario promedio de los meses anteriores."""
    if stats["baseline_days"] == 0:
        return None

    best = None
    for cat_id, total in stats["current_totals"].items():
        baseline_total = stats["baseline_totals"].get(cat_id, Decimal("0"))
        if baseline_total <= 0 or total < _CATEGORY_SPIKE_MIN_AMOUNT:
            continue
        current_rate = total / stats["days_elapsed"]
        baseline_rate = baseline_total / stats["baseline_days"]
        if baseline_rate <= 0:
            continue
        diff_pct = (current_rate - baseline_rate) / baseline_rate * 100
        if diff_pct < _CATEGORY_SPIKE_DIFF_PCT:
            continue
        if best is None or diff_pct > best[0]:
            best = (diff_pct, cat_id)

    if best is None:
        return None
    diff_pct, cat_id = best
    return {
        "dedupe_key": f"{workspace.id}:category_spike:{cat_id}:{today.year}-{today.month:02d}",
        "title": "Un gasto que se disparó este mes",
        "body": (
            f"Vas gastando en {stats['names'][cat_id]} a un ritmo {diff_pct:.0f}% más alto "
            f"que tu promedio de los últimos {_SPIKE_LOOKBACK_MONTHS} meses."
        ),
    }


def _insight_frequency_spike(workspace, today, stats):
    """"Comprás más seguido" -- cuántas veces compraste en una categoría
    este mes vs. tu frecuencia promedio, más allá de cuánta plata fue (el
    monto puede esconder un cambio de hábito -- ver `_insight_category_spike`
    para el monto)."""
    if stats["baseline_days"] == 0:
        return None

    best = None
    for cat_id, count in stats["current_counts"].items():
        if count < _FREQUENCY_SPIKE_MIN_COUNT:
            continue
        baseline_count = stats["baseline_counts"].get(cat_id, 0)
        if baseline_count == 0:
            continue
        current_rate = count / stats["days_elapsed"]
        baseline_rate = baseline_count / stats["baseline_days"]
        if baseline_rate <= 0:
            continue
        diff_pct = (current_rate - baseline_rate) / baseline_rate * 100
        if diff_pct < _FREQUENCY_SPIKE_DIFF_PCT:
            continue
        if best is None or diff_pct > best[0]:
            best = (diff_pct, cat_id, count)

    if best is None:
        return None
    diff_pct, cat_id, count = best
    return {
        "dedupe_key": f"{workspace.id}:frequency_spike:{cat_id}:{today.year}-{today.month:02d}",
        "title": "Estás comprando más seguido en una categoría",
        "body": (
            f"Hiciste {count} compras en {stats['names'][cat_id]} este mes, {diff_pct:.0f}% "
            f"más seguido que tu promedio de los últimos {_SPIKE_LOOKBACK_MONTHS} meses."
        ),
    }


def behavior_insights(workspace, user, today=None):
    """Detecta patrones de comportamiento de gasto para `workspace` (ver
    comentario de sección más arriba). No corre con menos de
    `INSIGHTS_MIN_HISTORY_DAYS` días de historial -- evita "patrones"
    armados con 3 transacciones."""
    today = today or timezone.localdate()
    qs = visible_transactions(workspace, user)
    first_date = qs.order_by("date").values_list("date", flat=True).first()
    if first_date is None or (today - first_date).days < INSIGHTS_MIN_HISTORY_DAYS:
        return []

    rate_map = get_rate_map(workspace)
    iso_year, iso_week, _ = today.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"
    stats = _category_month_stats(qs, rate_map, today)

    results = [
        _insight_weekend(workspace, qs, rate_map, today, first_date, week_label),
        _insight_post_income(workspace, qs, rate_map, today, first_date, week_label),
        _insight_small_purchases(workspace, qs, rate_map, today, first_date, week_label),
        _insight_peak_day(workspace, qs, rate_map, today, first_date, week_label),
        _insight_category_spike(workspace, today, stats),
        _insight_frequency_spike(workspace, today, stats),
    ]
    return [r for r in results if r is not None]
