# Carteras (`apps.accounts`)

## Propósito

Modela dónde vive el dinero y la deuda: cuentas bancarias, efectivo,
tarjetas de crédito, ahorro con meta, activos (propiedad, vehículo,
inversión) y deudas/préstamos, todo bajo un único modelo `Wallet`. La app
sigue llamándose `accounts` por compatibilidad de `app_label` en las
migraciones (el modelo unificó lo que antes eran `Account`, `SavingsGoal`,
`ReserveFund`, `Asset`, `Liability` y `Debt`), pero tanto el modelo como la
API se llaman `Wallet` / `/api/v1/wallets/`. Sirve tanto para el uso diario
(saldo, historial) como para lo financiero más avanzado: proyección de
metas de ahorro, interés compuesto, y estado de cuenta de tarjeta de
crédito.

## Modelos principales

- **`Wallet`**: el modelo central.
  - `purpose`: spending / savings / debt / asset -- para qué es la cartera.
  - `kind`: bank / credit / cash / custom -- subtipo dentro del `purpose`,
    sobre todo para iconografía y para saber si aplica `credit_limit`.
  - `parent`: jerarquía de un nivel (una cartera "grupo" con carteras hijas
    debajo). El saldo mostrado del padre es la suma del suyo propio más el
    de todos sus descendientes (`aggregated_balance`).
  - `opening_balance` / `current_balance`: el saldo de apertura es el punto
    de partida fijo y editable; `current_balance` es el saldo *propio* (sin
    hijos), mantenido incrementalmente por signals sobre `Transaction` y
    recalculable desde cero con `recompute_wallet_balance` /
    `manage.py recompute_balances`.
  - Campos de ahorro: `goal_amount`, `goal_date`, `monthly_contribution`,
    `savings_interest_rate` (+ `savings_interest_rate_period` que dice si
    la tasa cargada ya es anual o mensual, y `savings_interest_compounding`
    que dice cada cuánto se capitaliza).
  - Campos de tarjeta/deuda: `credit_limit`, `card_last4`,
    `billing_cycle_day` (día del corte), `payment_due_day`, `interest_rate`,
    `due_date`, `counterparty` (persona/entidad de una deuda), `bank_schema`
    (para auto-detectar a qué cartera aplica un correo bancario entrante) y
    `card_product` (producto de tarjeta del catálogo de lealtad, de donde
    salen los programas de puntos/cashback aplicables).
  - `visibility` (shared / private) + `owner`: ver reglas de negocio.
  - `is_archived`, `is_default` (a lo sumo una por workspace), `sort_order`.

- **`WalletCard`**: un número de plástico *adicional* de una `Wallet`
  (además de su `card_last4` principal). Modela el caso de una tarjeta
  titular + adicionales que comparten una sola cuenta -- un saldo, un
  límite, un estado de cuenta -- por eso no son carteras separadas ni una
  relación padre/hijo (esa es para carteras con saldo propio de verdad).
  Sirve para que una notificación de compra por correo caiga en la `wallet`
  correcta sin importar con cuál plástico se pagó.

## Endpoints

Todo bajo `/api/v1/wallets/`, scoping al workspace activo vía
`WorkspaceScopedViewSet`.

- **CRUD estándar**: crear/editar/listar/borrar (soft-delete) carteras.
  El listado excluye archivadas salvo que se pida `?is_archived=` explícito,
  y filtra por `purpose`, `kind`, `parent`, `is_active`, `is_archived`,
  `counts_toward_net_worth`.
- **`POST {id}/archive/` / `POST {id}/unarchive/`**: oculta o muestra una
  cartera de la lista sin borrarla -- sigue contando para el patrimonio
  neto. Archivar también le quita `is_default`.
- **`POST {id}/split/`**: convierte una cartera con actividad propia en un
  grupo, creando una cuenta hija nueva que hereda esa actividad (ver reglas
  de negocio).
- **`GET {id}/projection/`**: proyección de meta de ahorro o de pago de
  deuda (`services.goal_projection`). 404 si la cartera no aplica.
- **`GET {id}/statement/`**: estado de cuenta de una tarjeta de crédito a
  una fecha dada (`?as_of=`, hoy por defecto).
- **`GET {id}/interest-projection/`**: interés estimado a ganar en un mes
  dado (`?year=&month=`) para una cartera de ahorro con tasa configurada.
- **`GET statements/`**: `statement` de todas las tarjetas de crédito
  visibles del workspace (siempre a hoy), para el listado de Herramientas.
- **`POST reorder/`**: fija `sort_order` según una lista de ids.

## Reglas de negocio y decisiones no obvias

**Una cartera con hijas es un contenedor puro; no puede tener actividad
propia.** El saldo que se muestra de una cartera padre es la suma de sus
hijas (`aggregated_balance`), nunca un saldo propio real -- por eso
`_reject_if_group_wallet` (en `apps.transactions.api`) impide cargarle una
transacción, recurrente o compra a plazo directamente. Al revés, tampoco se
le puede agregar una hija a una cartera que **ya** tiene saldo o
movimientos propios (`WalletSerializer.validate_parent` lo chequea contra
`opening_balance`, `Transaction`, `RecurringExpense` e
`InstallmentPurchase`): habría que "dividirla" primero (`split`) para no
dejar esa actividad escondida detrás del nuevo agregado. `validate_parent`
también recorre la cadena de padres para impedir ciclos.

**`split`: cómo una cartera con actividad se convierte en grupo.** Crea una
cartera hija nueva con el mismo `purpose`/`kind`/moneda/etc., y le
**reasigna** (no recrea) toda la actividad de la original: transacciones
(como origen y como destino), gastos recurrentes y compras a plazo, vía
`UPDATE` de la FK. Si la original era la cartera `is_default` del
workspace, ese estado pasa a la hija. Al final, la original queda con
`opening_balance = 0` y ambos saldos se recalculan desde cero
(`recompute_wallet_balance`) -- así la cartera padre pasa a mostrar
únicamente la suma de sus hijas. Es lo inverso de fusionar (ver
`merge_wallet_into` más abajo).

**Carteras privadas vs. compartidas.** `visibility=private` + `owner`
existe para que un miembro del workspace tenga carteras que el resto no ve
(p. ej. una cuenta personal en un workspace de pareja/familia). El
serializer fuerza la coherencia: si se marca `private` sin `owner`, el
`owner` pasa a ser el usuario autenticado; si se marca `shared`, `owner` se
limpia siempre. El filtrado por visibilidad se repite en varios lugares
(`WalletViewSet.get_queryset`, `_visible_wallets` y
`services.visible_transactions` en `transactions`, `credit_card_statements_summary`)
porque cada uno consulta un modelo distinto (`Wallet` o `Transaction`) --
todos aplican la misma regla: `visibility=shared` **o** `owner=request.user`.

**Interés de ahorro: saldo diario ponderado, no saldo promedio simple.**
`savings_interest_projection` reconstruye el saldo real de cada día del mes
(desde las transacciones vivas, igual que `_balance_as_of`) y le aplica la
tasa diaria, sumando también cualquier interés ya capitalizado en días
anteriores según `savings_interest_compounding` (diaria/quincenal/mensual/
anual) -- el interés capitalizado gana interés a su vez. Para los días que
todavía no ocurrieron (mes en curso o futuro) asume que el saldo se
mantiene igual al último real conocido: no proyecta depósitos o retiros que
el usuario todavía no hizo. La tasa siempre se normaliza a anual
internamente (`_savings_annual_rate`) sea cual sea el período en que el
usuario la cargó (`savings_interest_rate_period`), justamente para no
mezclar semánticas con `interest_rate` (la tasa de una deuda/tarjeta, que
se usa distinto en `_months_to_payoff`).

**Proyección de meta: usa el ritmo real observado, no lo que el usuario
dijo que iba a aportar.** `goal_projection` calcula el "ritmo" como el
promedio de aporte/pago neto mensual de los últimos meses de movimientos
reales de la cartera. `monthly_contribution` (lo que el usuario declaró que
iba a aportar) solo se usa de respaldo cuando todavía no hay ningún
historial. En una deuda con `interest_rate` configurada, los meses para
saldarla se calculan con amortización real (`_months_to_payoff`) en vez de
un simple `restante / ritmo`, porque el interés compuesto alarga el plazo
real; si el pago ni siquiera cubre el interés del mes, la deuda nunca baja
con ese ritmo y la función devuelve `None` en vez de un número engañoso.

**Estado de cuenta de tarjeta: cuotas "vencidas" por el corte, no por el
calendario de la compra.** `credit_card_statement` calcula el pago de
contado como:

```
pago_de_contado = saldo_usado - capital_a_plazo_aún_no_vencido
```

`saldo_usado` es `-_balance_as_of(wallet, as_of)` -- lo mismo que
`current_balance` cuando `as_of` es hoy, así que si el saldo cacheado es
correcto, el pago de contado también lo es. Cada `InstallmentPurchase` bajó
el disponible completo al registrarse (es una `Transaction` normal por el
total), pero al corte solo se debe lo que ya venció -- por eso el capital
a plazo que aún no vence se **resta** del saldo usado, para no pedirle al
usuario que adelante cuotas futuras. Una cuota `n` vence en su `n`-ésimo
corte desde la compra (`installment_schedule`), calculado puramente sobre
`billing_cycle_day`, sin ningún contador manual de por medio. El día de
corte/pago se recorta al último día real del mes cuando no existe (p. ej.
corte "el 31" en febrero), igual que hacen los bancos.

**Fusionar carteras (`manage.py merge_wallet_into`) es la operación
inversa de `split`, pensada para el error de crear una cartera aparte para
una tarjeta adicional.** Por defecto se niega a fusionar si la cartera
origen tiene actividad propia (transacciones, recurrentes, cuotas, hijas) --
solo con `--move-activity` se reasigna esa actividad al destino, y ahí
conviene `--dry-run` primero porque mover plata de una cartera a otra sin
que el usuario vea exactamente qué se movió es el tipo de cosa que
conviene revisar antes de ejecutar, no después. Se niega igual (con o sin
`--move-activity`) si ya existe una transacción o recurrente que conecta
origen y destino entre sí (p. ej. un pago de una tarjeta con la otra):
fusionarlas dejaría una fila con `wallet == to_wallet`, que no tiene
sentido y hay que resolver a mano. Si el destino resultaba ser hija del
origen, su `parent` se sube un nivel (al padre del origen, o sin padre) para
no dejarla apuntando a una cartera recién soft-eliminada.

**Diagnóstico de tarjeta (`manage.py diagnose_credit_card`).** Existe
porque `current_balance` es un valor cacheado mantenido por signals -- si
diverge de lo recalculado desde los movimientos (algo que el comando
señala explícitamente con `<<< NO COINCIDE` y sugiere correr
`recompute_balances`), el estado de cuenta completo queda mal. Imprime,
para una tarjeta, el desglose línea por línea de qué mueve el saldo, el
estado de cada compra a plazo y la reconciliación `total_due = saldo_usado
- capital_a_plazo_aún_no_vencido`, para poder comparar contra la banca en
línea real del banco.

**Nota / algo no del todo claro:** no se encontró en `apps.accounts` ni en
`apps.transactions` ninguna conversión de moneda al mover dinero entre
carteras de distinta moneda en una transferencia -- el comentario en
`Transaction.save()` lo confirma ("conversión automática ahí: fuera de
alcance por ahora"), pero no quedó claro en esta lectura qué le impide hoy
a un usuario crear una transferencia entre una cartera en USD y otra en
otra moneda sin ningún ajuste de tasa; valdría la pena revisar si hay una
validación en otra capa (frontend) que lo bloquee, porque a nivel de
`TransactionSerializer`/`Wallet` no se vio ninguna.
