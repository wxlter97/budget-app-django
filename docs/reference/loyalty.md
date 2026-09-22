# Lealtad de tarjetas (puntos, cashback, descuentos)

## Propósito

Modela los programas de recompensas de las tarjetas de crédito/débito de los
usuarios (puntos, cashback, descuentos) y calcula automáticamente qué generó
cada gasto real, según la tarjeta con la que se pagó, el rubro del gasto y
reglas especiales (comercio, día de la semana, cargos automáticos). Resuelve
"¿cuánto me da esta tarjeta si compro esto, en este comercio, este día?" sin
que el usuario tenga que hacer la cuenta a mano, y lleva el libro de lo que ya
ganó, canjeó o le ajustaron.

Es función Pro: se gatea con `require_feature_for_workspace(workspace,
"loyalty")` (ver `apps/billing/services.py`) en todo lo que expone lo ganado
por workspace. El catálogo (bancos, productos, programas) no se gatea porque
es global y de solo lectura para cualquier autenticado.

## Modelos principales

- **`Bank`** / **`CardProduct`**: catálogo de bancos y productos de tarjeta
  (ej. "BAC" → "Visa Signature"). Global, no por workspace.
- **`CategoryType`**: rubro estándar (Supermercado, Gasolina, Restaurantes...)
  independiente de las categorías reales de cada workspace
  (`transactions.Category`). Existe porque las tasas de un programa se
  definen sobre un catálogo fijo de rubros, no sobre las categorías libres de
  cada usuario; cada `Category` se mapea a un `CategoryType` para heredar la
  tasa que le toca (`Category.category_type`).
- **`Merchant`**: un comercio conocido (Súper Selectos, Uber Eats...). Sirve
  para dos cosas que un rubro solo no cubre: (1) beneficios de un solo
  comercio (7% en Selectos) y (2) un rubro más fino que la categoría del
  usuario (una categoría "Comida" mezcla restaurantes con supermercados). El
  reconocimiento es por `aliases` contra la descripción libre de la
  transacción (`services.match_merchant`), no hay campo de comercio en
  `Transaction`.
- **`LoyaltyProgram`**: un mecanismo de recompensa de un `CardProduct`
  (`kind` = points/cashback/discount), con una `default_rate` y, opcional, un
  `min_amount` (compra mínima para ganar). Un mismo producto puede tener
  varios programas a la vez.
- **`LoyaltyCategoryRate`**: tasa especial de un programa para un rubro **o**
  un comercio puntual, opcionalmente sólo un día de la semana y/o sólo para
  cargos automáticos (`requires_autopay`). Es la pieza que resuelve "5% en
  Supermercado en vez del 1% default".
- **`LoyaltyEarning`** (`apps.loyalty`, pero referenciado también desde
  `apps.transactions`): lo que efectivamente generó una `Transaction` real
  según un programa — por workspace. Puntos y cashback se recalculan solos
  con la señal de `Transaction`; el descuento lo registra el propio cliente.
- **`LoyaltyMovement`**: canjes y ajustes sobre lo ya ganado (el "libro" de
  movimientos de recompensas). Nunca sobrescribe un saldo: agrega un renglón.

## Endpoints

Todos bajo `/api/v1/` vía el router (`config/api_router.py`).

**Catálogo global** (lectura: cualquier autenticado; escritura: sólo staff,
`IsAdminOrReadOnly`):
- `banks/`, `category-types/`, `loyalty-merchants/`, `card-products/`,
  `loyalty-programs/`, `loyalty-category-rates/` — CRUD estándar
  (`ModelViewSet`).

**Por workspace** (requieren `X-Workspace-ID` + membresía, y la función Pro
`loyalty`):
- `loyalty-earnings/` (`LoyaltyEarningViewSet`) — sólo lectura; lo que ganó
  cada transacción. Filtra por `kind`, `program`, `wallet`.
  - `GET loyalty-earnings/summary/` — saldo de puntos por cartera + cashback
    ganado / descuento ahorrado en un rango (`?date_after=&date_before=`).
- `loyalty-movements/` (`LoyaltyMovementViewSet`) — canjear (`kind=redeem`) o
  ajustar (`kind=adjust`) el disponible de un programa en una cartera.
  Permite editar (`PATCH`, sólo ajustes) y deshacer (`DELETE`, que revierte
  también el ingreso depositado si lo había).

## Reglas de negocio y decisiones no obvias

- **Catálogo global vs. por workspace**: `Bank`, `CardProduct`,
  `LoyaltyProgram`, `CategoryType`, `LoyaltyCategoryRate` son globales (sin
  `workspace`) — es lo único en el dominio que puede serlo, porque son
  configuración de producto, no datos del usuario. `LoyaltyEarning` y
  `LoyaltyMovement` sí son por workspace.
- **Resolución de tasa** (`LoyaltyProgram.rate_for`): gana la regla más
  específica, en este orden: (1) comercio + día, (2) comercio sin día, (3)
  rubro + día, (4) rubro sin día, (5) `default_rate` del programa. Las reglas
  `requires_autopay` sólo cuentan si el gasto es un cargo automático, y en
  ese caso ganan a las de cualquier cargo.
- **Rubro del gasto**: si el comercio se reconoce en la descripción, su
  rubro manda sobre el de la categoría (`signals._recompute`) — una
  categoría "Comida" mezcla rubros que un comercio reconocido desambigua.
  Si el usuario ya asignó un `merchant` a mano en la transacción, eso manda
  sobre lo que se deduzca del texto.
- **Puntos y cashback se recalculan enteros, no a delta**: cada `post_save`
  de `Transaction` borra y recrea los `LoyaltyEarning` automáticos
  (`kind` points/cashback) de esa transacción (`signals.sync_loyalty_earnings`
  → `_recompute`). A esta escala el costo no importa, y así cualquier cambio
  (monto, categoría, cartera) queda reflejado sin casos especiales. Cubre
  soft-delete (llega como `save` con `is_deleted=True`) y borrado físico
  (`post_delete`).
- **El descuento es distinto**: no lo calcula la señal, lo registra el
  cliente al crear/editar la transacción (`TransactionSerializer._sync_discount`
  en `apps/transactions/api.py`), porque necesita el monto **antes** del
  descuento — algo que la transacción ya guardada (con el monto neto) no
  puede reconstruir. `LoyaltyEarning.original_amount` guarda ese monto
  previo, y lo ahorrado se calcula al vuelo (`discount_saved_amount =
  original_amount - transaction.amount`) para no desincronizarse si se edita
  el monto después.
- **Recompute manual**: cuando se carga o cambia el catálogo (nuevo rubro,
  nueva tasa) *después* de que los gastos ya existían, la señal no vuelve a
  correr sola — hay que llamar `services.recompute_earnings(...)` (o el
  management command `recompute_loyalty_earnings`) explícitamente.
- **Disponible = ganado + movimientos**: `services.available_for` /
  `wallet_balances` no guardan un saldo, lo derivan sumando lo ganado
  (`LoyaltyEarning`) más los movimientos (`LoyaltyMovement.delta`, negativo en
  canjes). Un canje o ajuste nunca puede vivir en `LoyaltyEarning` porque ese
  modelo se recalcula entero con cada gasto y se perdería.
- **Canje con depósito**: al canjear (`services.redeem`) se puede indicar una
  `deposit_wallet` para acreditar el valor en dinero como un ingreso real
  (categoría "Recompensas", creada on-demand por
  `get_or_create_rewards_category`). Deshacer ese movimiento
  (`undo_movement`) también hace soft-delete del ingreso, para no dejar la
  cartera desalineada con el libro.
- **Gate de función Pro**: `require_feature_for_workspace(workspace,
  "loyalty")` se aplica en `LoyaltyEarningViewSet` y `LoyaltyMovementViewSet`
  (no en el catálogo). El mismo gate se repite en
  `TransactionSerializer._loyalty_enabled` para no mostrar `loyalty_earnings`
  en el payload de una transacción si el workspace no tiene la función — pero
  la transacción sigue guardando sus `LoyaltyEarning` igual por dentro (no se
  pierde nada si el usuario luego pasa a Pro).
