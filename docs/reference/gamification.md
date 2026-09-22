# Gamificación

## Propósito

Da retroalimentación positiva sobre el hábito de gasto de un workspace:
racha de días sin salirse del presupuesto, fines de semana sin gastos, %
de ahorro mensual, y un catálogo fijo de badges por hitos. No es un sistema
de puntos ni tiene efecto en ningún otro módulo (no interactúa con
`apps.loyalty`, que es de recompensas de tarjetas) — es puramente
motivacional para el usuario.

## Modelos principales

- **`Badge`**: catálogo fijo de logros posibles (código, nombre,
  descripción, ícono), sembrado por una migración de datos
  (`0002_seed_badges.py`). Igual para todos los workspaces.
- **`WorkspaceBadge`**: qué badge ya ganó un workspace, y cuándo. Es lo único
  que efectivamente crece por workspace; se otorga de forma perezosa (en
  cada consulta del resumen), no hay tarea periódica dedicada.

Deliberadamente **no hay** modelo de "racha" ni de "día sin gasto": ambos se
calculan al vuelo desde `Transaction` (`services.py`) en cada request, para
no duplicar la fuente de verdad — si se corrige o hace backfill de
transacciones viejas, una racha guardada aparte quedaría desincronizada.

## Endpoints

- `GET /api/v1/gamification/summary/` (`GamificationSummaryView`) — único
  endpoint. Devuelve racha actual, racha máxima histórica, cantidad de fines
  de semana sin gastos, % de ahorro del mes en curso, y el estado
  (ganado/no) de cada badge del catálogo. De paso, otorga cualquier badge
  nuevo que ya se haya ganado (evaluación perezosa).

## Reglas de negocio y decisiones no obvias

- **Qué es un "no-spend day"**: un día sin ninguna transacción de gasto que
  cuente para el presupuesto (`type=expense, counts_toward_budget=True`).
  Una transferencia a una cartera de ahorro, o un gasto marcado
  explícitamente fuera de presupuesto, **no** rompen la racha.
- **Racha actual vs. máxima**: `current_streak` cuenta hacia atrás desde hoy
  (o una fecha dada) hasta el primer día con gasto, sin ir más atrás de la
  fecha de creación del workspace. `longest_streak` recorre toda la historia
  del workspace día por día — es O(días desde creación), aceptable a esta
  escala pero vale saberlo si el workspace lleva años de antigüedad.
- **% de ahorro mensual** (`monthly_savings_percentage`): a diferencia de la
  racha, acá sí cuentan **todos** los movimientos reales, incluidos los
  gastos marcados fuera de presupuesto — es el ahorro de caja real
  `(ingresos - gastos) / ingresos`, no envelope budgeting. Devuelve `None`
  sin ingresos ese mes (el porcentaje no tiene sentido con denominador cero).
- **Otorgamiento idempotente y perezoso**: `evaluate_and_award_badges` se
  llama en cada `summary()` (no hay Celery beat ni cron para esto). Usa
  `bulk_create(..., ignore_conflicts=True)` contra el `UniqueConstraint`
  `(workspace, badge)`, así que evaluarlo de más no duplica nada.
- **Umbrales fijos en código**: los umbrales de cada badge (7/30/100 días de
  racha, 10%/20% de ahorro, 1+ fin de semana sin gastos) están hardcodeados
  en `evaluate_and_award_badges`, no son configurables desde el catálogo
  `Badge` — agregar un badge nuevo requiere tocar código además de la fila.
