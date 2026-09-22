# Reports

## Propósito

Toda la capa de agregación y cálculo financiero de un workspace: presupuesto
vs. gasto real, patrimonio neto, flujo de caja mensual, tendencias de gasto
por categoría, el resumen de la pantalla principal, "Programado" (lo que
vence a futuro sin haberse registrado todavía) y el histórico mensual
(`MonthlySnapshot`). Es prácticamente toda lectura: agrega datos de
`transactions`/`accounts` ya existentes, sin ser dueña de ningún dato
transaccional propio (la única excepción es `MonthlySnapshot`, y hasta ese lo
genera un job, no el usuario).

También vive acá `behavior_insights`, el detector de patrones de
comportamiento de gasto ("gastás más los fines de semana", "gasto hormiga",
etc.) que alimenta las notificaciones de `apps.notifications` -- ver ese doc
para cómo se convierten en avisos.

## Modelos principales

- **`MonthlySnapshot`**: fotografía del estado financiero de un workspace al
  cierre de un mes (`total_net_worth`, `total_income`, `total_expenses`).
  Único por (workspace, year, month). Lo genera Celery Beat el día 1 a las
  00:05 (`close_month`), nunca el cliente -- alimenta el historial mensual de
  la app (`NetWorthHistoryScreen`). Es un concepto de calendario puro,
  siempre mensual, independiente de `workspace.budget_period` (que puede ser
  semanal, etc.).

El resto de la app no tiene modelos propios: `budget_vs_actual` lee
`CategoryBudget`/`CategoryProvision` (de `apps.transactions`), `net_worth_*`
lee `Wallet` (de `apps.accounts`), `upcoming_scheduled` lee
`RecurringExpense`/`InstallmentPurchase`/`Wallet`.

## Endpoints

Todos bajo `/api/v1/reports/` salvo `monthly-snapshots/` (va por el router
como recurso normal) y `dashboard/balance/` (aparte, ver más abajo). Todos
requieren `X-Workspace-ID` + membership, salvo el último.

- **`GET /reports/budget/`** (`BudgetReportView`): presupuesto vs. gasto real
  por categoría para el período de `workspace.budget_period` que contiene a
  `?period_start=` (default hoy). Devuelve filas por categoría (`budgeted`,
  `spent`, `remaining`, `provision` -- el sobrante acumulado de períodos
  anteriores, ver `close_previous_budget_period`) y agrupadas por categoría
  padre (`groups`), más totales.
- **`GET /reports/net-worth/`** (`NetWorthView`): patrimonio neto actual +
  desglose por `Wallet.purpose` (spending/savings/debt/asset). Requiere
  feature `net_worth` del plan.
- **`GET /reports/cashflow/`** (`CashflowView`): serie mensual de
  ingresos/gastos/neto, `?months=` (default 6, máx 24). Requiere feature
  `advanced_reports`.
- **`GET /reports/category-trends/`** (`CategoryTrendsView`): gasto mensual
  por categoría de los últimos N meses (`?months=`) + qué categoría más
  creció/bajó entre el mes en curso y el anterior. Requiere feature
  `advanced_reports`.
- **`GET /reports/summary/`** (`DashboardSummaryView`): resumen de la
  pantalla principal -- mes en curso (ingresos/gastos/neto), patrimonio neto
  (`null` si el plan no tiene la feature `net_worth`, no se omite el campo),
  correos bancarios pendientes de revisar, y top 5 categorías de gasto del
  mes. Es la única llamada que combina varias de las anteriores en una sola
  ida (`dashboard_summary` reusa un solo `rate_map`).
- **`GET /reports/scheduled/`** (`ScheduledView`): ver sección dedicada
  "Programado" más abajo.
- **`monthly-snapshots/`** (`MonthlySnapshotViewSet`, sólo lectura): histórico
  mensual del workspace. Requiere feature `net_worth_history`.
- **`GET /dashboard/balance/`** (`DashboardBalanceView`, `config/urls.py` ->
  `apps.reports.dashboard_api`): endpoint aparte para "Villa Wxlter" (un
  dashboard externo, sin usuario/sesión). Auth por token fijo
  (`DASHBOARD_API_TOKEN` en `Authorization: Bearer`), no JWT. Expone sólo el
  patrimonio neto de un workspace fijo (`DASHBOARD_WORKSPACE_ID`, o el único
  workspace existente si no está seteado). No confundir con el resto del
  API: es un caso de uso muy específico y deliberadamente aislado.

## Reglas de negocio y decisiones no obvias

### "Programado" (`upcoming_scheduled`) y su vínculo con `transactions`

`services.upcoming_scheduled(workspace, user, until=None, since=None)` (línea
~340 de `services.py`) calcula, **sin crear ni modificar nada**, todas las
ocurrencias futuras de:

- gastos/ingresos/transferencias recurrentes activos (`RecurringExpense`),
  proyectando desde `next_due_date` con la misma función de avance
  (`apps.transactions.services._advance`) que usa el job real;
- cuotas de compras a plazo (`InstallmentPurchase`) todavía no vencidas
  (según `installment_status`, que las calcula sobre los cortes de tarjeta,
  no un contador propio);
- el próximo vencimiento de estado de cuenta de cada tarjeta de crédito (un
  solo ítem, sólo del ciclo ya cortado o por cortar -- nunca proyecta ciclos
  futuros, porque su monto depende de gasto que todavía no pasó);
- vencimientos puntuales de deudas con `Wallet.due_date` fijo.

Esto alimenta la tarjeta "PROGRAMADO" del dashboard, los marcadores de la
lista de transacciones y el calendario financiero. Es **de sólo lectura por
diseño**: no toca `RecurringExpense.next_due_date` ni crea ninguna
`Transaction`. El endpoint (`ScheduledView`) no tiene gate de plan a
propósito: alimenta también partes que sí están en el plan gratis (la
tarjeta y los marcadores), así que gatearlo rompería esas dos cosas para
todo el mundo -- el gate de la pestaña Calendario, si el plan no la
incluye, se hace del lado del cliente (frontend `moneyapp`, `dashboard.tsx`),
no acá.

**El vínculo con `apps.transactions`** (agregado recientemente para evitar
duplicados con el job automático de recurrentes): cuando el usuario toca un
ítem "Programado" de tipo `recurring` y lo registra a mano como una
`Transaction` real (en vez de esperar a que corra el job diario), el cliente
manda el campo `recurring_expense` al crear la transacción
(`TransactionViewSet`/`TransactionSerializer.create`, `apps/transactions/
api.py` línea ~653). Eso dispara
`apps.transactions.services.register_manual_recurring_occurrence(rec, date)`,
que:

1. Toma un lock (`select_for_update`) sobre ese `RecurringExpense`.
2. Avanza `next_due_date` hacia adelante (con la misma `_advance`) hasta que
   quede **después** de la fecha que el usuario acaba de registrar.
3. Guarda `next_due_date` sólo si cambió.

Sin este paso, `next_due_date` se quedaría como estaba y
`generate_recurring_transactions` (el job de Celery que sí crea
`Transaction`s automáticamente) volvería a generar esa misma ocurrencia al
día siguiente -- antes de este cambio, el alta manual desde "Programado" sólo
prellenaba el formulario con los datos del recurrente, pero nunca tocaba la
regla en sí, así que quedaba duplicada apenas corría el job. Al **editar**
una transacción ya creada (no crearla), `TransactionSerializer.update`
descarta explícitamente `recurring_expense` -- ese avance sólo aplica al
alta, nunca a una edición posterior.

En síntesis: `apps.reports.services.upcoming_scheduled` sólo **lee y
proyecta** sobre `next_due_date`; quien lo **modifica** es siempre
`apps.transactions.services` (o bien `register_manual_recurring_occurrence`
en el alta manual, o bien `generate_recurring_transactions` en el job
automático) -- `reports` nunca escribe en `RecurringExpense`.

### Conversión de moneda

Todos los reportes que suman montos de distintas carteras/transacciones usan
`apps.workspaces.currency.get_rate_map`/`convert` (ver `workspaces.md`): una
fila en una moneda sin `ExchangeRate` cargada se excluye del total en
silencio. `dashboard_summary` arma un solo `rate_map` y lo reutiliza en las
tres subconsultas (mes, patrimonio, top categorías) para no repetir la
lectura de tasas.

### Presupuesto: provisión y rollover

`budget_vs_actual` no sólo compara `budgeted` vs `spent`: suma también
`CategoryProvision.accumulated_amount`, el sobrante acumulado de períodos
anteriores donde se gastó menos de lo presupuestado.
`close_previous_budget_period` (tarea separada de `close_month`) hace ese
rollover período por período, guardando hasta dónde ya se procesó en
`workspace.budget_period_closed_through` -- a diferencia de `MonthlySnapshot`
(que sólo sobreescribe), acá el acumulado se **incrementa**, así que correr
esto dos veces sobre el mismo período duplicaría el sobrante si no fuera por
esa guarda. `_MAX_CATCHUP_PERIODS = 400` limita cuántos períodos atrasados se
procesan en una sola corrida (cubre un cron caído varios días incluso con
`budget_period=daily`, sin arriesgar un bucle larguísimo si el contador
quedara desalineado).

Una categoría **grupo** (con subcategorías) no debería tener presupuesto
propio aparte del de sus hijas (lo impide el serializer hacia adelante), pero
`budget_vs_actual` igual filtra cualquier resto de datos viejos para no
duplicar el total del grupo con el de sus subcategorías.

### `behavior_insights`: detección de patrones, no solo números

Seis detectores independientes (`_insight_weekend`, `_insight_post_income`,
`_insight_small_purchases`, `_insight_peak_day`, `_insight_category_spike`,
`_insight_frequency_spike`), cada uno devuelve `None` si no aplica o un dict
`{dedupe_key, title, body}`. No corren con menos de 30 días de historial de
transacciones (`INSIGHTS_MIN_HISTORY_DAYS`), para no armar "patrones" con
tres movimientos. El `dedupe_key` de cada uno decide la cadencia real del
aviso en `apps.notifications` (ver ese doc): los que miden algo EN CURSO
(fin de semana, día pico, post-cobro) dedupean por semana ISO; los que miden
un TOTAL/COMPARACIÓN del mes (gasto hormiga, categoría/frecuencia disparada)
dedupean por mes. Esta función vive en `reports` (no en `notifications`)
porque toda su matemática es sobre `Transaction`, igual que el resto de los
reportes -- `notifications` sólo la consume y decide cuándo/a quién avisar.

### Visibilidad de carteras privadas

`spending_by_category`, `budget_vs_actual`, `monthly_cashflow`,
`net_worth_breakdown` y `upcoming_scheduled` reciben `user` y filtran
carteras/transacciones privadas (`Wallet.visibility`) que no le pertenecen a
ese usuario (`_visible`/`_wallet_ok`). Esto significa que dos miembros del
mismo workspace pueden ver totales agregados **distintos** en los mismos
endpoints si hay carteras privadas de por medio -- no es un bug, es el
comportamiento esperado de "cartera privada".
