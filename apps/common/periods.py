"""
Aritmética de "período de presupuesto": una preferencia global por workspace
(``Workspace.budget_period``) que reemplaza el "mes calendario" fijo que
tenía antes ``CategoryBudget`` -- ahora puede ser diario, semanal,
quincenal, mensual o anual.

Todo período se identifica por su fecha de inicio (``period_start``, un
``date``) -- nunca por índice ni por año/mes sueltos -- así una sola columna
(`CategoryBudget.period_start`) sirve para los cinco tipos sin necesitar
columnas distintas por caso. Las quincenas son de calendario (1-15 y
16-fin de mes), no una ventana rodante de 14 días: así siempre caen dentro
de un solo mes, más fácil de ubicar para el usuario que contar desde una
fecha ancla arbitraria.
"""
from calendar import monthrange
from datetime import date, timedelta

DAILY = "daily"
WEEKLY = "weekly"
BIWEEKLY = "biweekly"
MONTHLY = "monthly"
YEARLY = "yearly"

CHOICES = [
    (DAILY, "Diario"),
    (WEEKLY, "Semanal"),
    (BIWEEKLY, "Quincenal"),
    (MONTHLY, "Mensual"),
    (YEARLY, "Anual"),
]

VALID = {c for c, _ in CHOICES}


def _check(period: str) -> None:
    if period not in VALID:
        raise ValueError(f"Período de presupuesto desconocido: {period!r}")


def period_start(d: date, period: str) -> date:
    """Fecha de inicio del período de tipo `period` que contiene a `d`."""
    _check(period)
    if period == DAILY:
        return d
    if period == WEEKLY:
        return d - timedelta(days=d.weekday())  # lunes de esa semana
    if period == BIWEEKLY:
        return d.replace(day=1) if d.day <= 15 else d.replace(day=16)
    if period == MONTHLY:
        return d.replace(day=1)
    return d.replace(month=1, day=1)  # YEARLY


def period_end(start: date, period: str) -> date:
    """Último día (inclusive) del período que arranca en `start`."""
    _check(period)
    if period == DAILY:
        return start
    if period == WEEKLY:
        return start + timedelta(days=6)
    if period == BIWEEKLY:
        last_day = monthrange(start.year, start.month)[1]
        return start.replace(day=15) if start.day == 1 else start.replace(day=last_day)
    if period == MONTHLY:
        last_day = monthrange(start.year, start.month)[1]
        return start.replace(day=last_day)
    return start.replace(month=12, day=31)  # YEARLY


def next_period_start(start: date, period: str) -> date:
    """`period_start` del período inmediatamente siguiente a `start`."""
    return period_end(start, period) + timedelta(days=1)


def previous_period_start(start: date, period: str) -> date:
    """`period_start` del período inmediatamente anterior a `start`."""
    return period_start(start - timedelta(days=1), period)


def label(start: date, period: str) -> str:
    """Etiqueta corta en español para mostrar el período (p. ej. "1-15 ago
    2026", "Semana del 10 ago", "Agosto 2026")."""
    _check(period)
    months = [
        "ene", "feb", "mar", "abr", "may", "jun",
        "jul", "ago", "sep", "oct", "nov", "dic",
    ]
    m = months[start.month - 1]
    if period == DAILY:
        return f"{start.day} {m} {start.year}"
    if period == WEEKLY:
        end = period_end(start, period)
        return f"Semana del {start.day} {m}" + (f"-{end.day}" if end.month == start.month else "")
    if period == BIWEEKLY:
        end = period_end(start, period)
        return f"{start.day}-{end.day} {m} {start.year}"
    if period == MONTHLY:
        full = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        return f"{full[start.month - 1].capitalize()} {start.year}"
    return str(start.year)  # YEARLY
