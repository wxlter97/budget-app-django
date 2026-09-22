# Transacciones (`apps.transactions`)

## Propósito

Es el corazón del dominio: modela los movimientos de dinero (`Transaction`)
y todo lo que sirve para organizarlos, presupuestarlos y automatizarlos --
categorías, etiquetas, presupuestos por categoría, gastos recurrentes,
compras a plazo, división de una transacción entre categorías o entre
personas, reembolsos e importación en lote desde Excel. Prácticamente todo
lo demás en el backend (reportes, patrimonio neto, lealtad, notificaciones,
IA, quick-add) lee o escribe transacciones a través de esta app.

## Modelos principales

- **`Category`**: jerarquía de exactamente 2 niveles, estilo Buddy. Una
  categoría sin `parent` es un *grupo* (bucket de presupuesto, p. ej.
  "Vivienda"); con `parent` es una categoría asignable a transacciones, y su
  `parent` siempre tiene que ser un grupo (nunca otra subcategoría) --
  validado en el serializer. `type` (income/expense) se hereda del padre al
  crear una subcategoría. `category_type` opcional la vincula a un rubro
  global del catálogo de lealtad (`apps.loyalty`), para heredar tasas de
  puntos/cashback/descuento.

- **`Tag`**: etiqueta libre, de un solo nivel, transversal a la categoría
  (p. ej. "viaje-cancún" agrupa comida + transporte + hospedaje). Se crea al
  vuelo por nombre (get-or-create case-insensitive) al escribirla en una
  transacción -- no hace falta un paso previo de "crear etiqueta".

- **`Transaction`**: el modelo central.
  - `type`: income / expense / transfer.
  - `wallet`: cartera origen siempre (de acá sale la plata, incluso en una
    transferencia). `to_wallet`: solo en transferencias, cartera destino.
  - `category`: requerida en income/expense, `null` en transferencias
    (aunque una transferencia puede llevar categoría opcional, p. ej. mover
    a "Ahorro").
  - `currency`: denormalizada de `wallet.currency` en cada `save()`, nunca
    la manda el cliente -- así nunca queda desincronizada de la cartera real
    y permite sumar/reportar sin mezclar monedas en un workspace multi-moneda.
  - `source`: de dónde vino (manual, email_import, recurring, installment,
    quick_add, excel_import, refund).
  - `is_recurring`, `installment_purchase`: marca de origen automático.
  - `is_refundable` (editable a mano, "espero que me devuelvan esto") vs.
    `is_refunded` + `refund_of` (de solo lectura, ver más abajo).
  - `paid_by` + `split_group`: dos mecanismos de división **independientes
    y coexistentes** -- `paid_by`/`TransactionShare` reparte el mismo monto
    entre *personas*; `split_group` reparte el monto entre *categorías*
    creando varias `Transaction` reales.
  - `tags`: M2M libre.
  - `receipt`: `FileField` (no `ImageField`, porque puede ser un PDF de un
    correo bancario), servido solo por `/transactions/{id}/receipt/` -- nunca
    por una URL directa del storage, para que quede protegido por la misma
    membresía de workspace que el resto del API.

- **`Person`**: alguien con quien se divide una transacción. `member`
  (una `Membership` real del workspace) es opcional; `name` siempre está,
  porque también puede ser un amigo externo sin cuenta.

- **`TransactionShare`**: la parte de `transaction` que le corresponde a
  `person` cuando se divide entre personas. Mientras `is_settled` sea
  `False`, `person` le debe `amount` a `transaction.paid_by`.

- **`CategoryBudget`**: monto presupuestado de una categoría en un
  `period_start`. La duración del período la fija `workspace.budget_period`
  *al momento de crear la fila* -- un `CategoryBudget` ya guardado no cambia
  de forma aunque el workspace cambie de preferencia de período después.

- **`CategoryProvision`**: acumulado de sobrante de una categoría que rueda
  mes a mes en vez de perderse (cuando el gasto real es menor al
  presupuesto), vía tarea programada de cierre de mes.

- **`RecurringExpense`**: regla que genera una `Transaction` cada vez que
  vence `next_due_date` (income/expense/transfer). El nombre del modelo
  quedó como "RecurringExpense" por compatibilidad de migraciones aunque ya
  no es solo gastos.

- **`InstallmentPurchase`**: compra a plazo. Solo sirve para el *cálculo*
  del estado de cuenta de la tarjeta -- no genera una `Transaction` por
  cuota (ver reglas de negocio).

- **`RecurringSuggestionDismissal`**: "no, gracias" a una sugerencia de
  "esto parece recurrente" (ver `detect_recurring_candidates`), identificada
  por categoría + cartera + monto aproximado, no por transacción puntual.

## Endpoints

Todo bajo `/api/v1/`, con scoping automático al workspace activo
(`WorkspaceScopedViewSet`: filtra por el header de workspace y hace
soft-delete en `DELETE`).

**Categorías** (`/categories/`, CRUD + extras)
CRUD estándar, más `POST reorder` (fija `sort_order` según una lista de
ids), `GET deleted` / `POST {id}/restore/` (papelera de soft-deletes) y
`DELETE {id}/purge/` (borrado físico definitivo, solo si ya estaba
soft-deleted y no tiene nada real colgando: subcategorías vivas,
presupuestos, recurrentes o compras a plazo asociadas).

**Etiquetas** (`/tags/`, CRUD + `GET summary`)
CRUD para listar/renombrar/borrar etiquetas (la asignación a una
transacción va aparte, por `tag_names` del serializer de `Transaction`).
`summary` da ingresos/gastos acumulados y cantidad de movimientos por
etiqueta, sin importar la categoría de cada uno.

**Personas y splits por persona** (`/people/`, `/transactions/{id}/split-people/`,
`/transactions/{id}/settle-share/{share_id}/`, `/transactions/balances/`,
`/transactions/settle-balance/`)
`PersonViewSet` administra las personas externas creadas a mano (los
miembros del workspace se agregan solos la primera vez que participan de un
split). `split-people` divide una transacción entre personas sin tocar su
monto/categoría; `settle-share` marca pagada la parte de una persona;
`balances` da el neto de quién le debe a quién en todo el workspace;
`settle-balance` salda de una vez toda la deuda neta entre dos personas.

**Transacciones** (`/transactions/`, CRUD + acciones)
CRUD estándar (list soporta filtros ricos por querystring vía
`TransactionFilter`: rango de fecha, rango de monto, tag, texto libre sobre
descripción/categoría/cartera, y `wallet` que matchea tanto si la cartera es
origen como destino). Además:
- `GET totals`: ingresos/gastos por moneda con los mismos filtros que la
  lista (existe porque la lista está paginada).
- `POST/GET/DELETE {id}/receipt/`: subir, descargar o borrar el comprobante.
- `POST {id}/register-refund/`: registra el reembolso real de un gasto.
- `POST {id}/split/`: divide la transacción entre categorías.
- `POST {id}/split-people/`: divide la transacción entre personas.
- `GET check-duplicate`: candidatas a duplicado antes de guardar a mano.
- `GET import-template` / `POST import`: plantilla .xlsx y carga en lote.

**Presupuestos por categoría** (`/category-budgets/`, CRUD + `POST set-forward`)
CRUD de `CategoryBudget`. `set-forward` fija el monto de un período y lo
propaga hacia adelante hasta el primer período que el usuario ya haya
personalizado con otro valor (ver reglas de negocio).

**Gastos recurrentes** (`/recurring-expenses/`, CRUD + `GET suggestions` +
`POST dismiss-suggestion`)
CRUD de la regla. `suggestions` expone `detect_recurring_candidates` (no
crea nada, solo sugiere); `dismiss-suggestion` la descarta para no
repetirla.

**Compras a plazo** (`/installment-purchases/`, CRUD)
CRUD; crear o editar sincroniza la única `Transaction` real de la compra
(ver reglas de negocio); borrar hace soft-delete de esa misma transacción.

## Reglas de negocio y decisiones no obvias

**Una cartera con hijas nunca recibe transacciones directas.** Una cartera
padre es puramente un contenedor: su saldo mostrado es la suma de sus hijas
(`Wallet.aggregated_balance`), nunca uno propio. `_reject_if_group_wallet`
(en `apps.transactions.api`) lo bloquea en todos los puntos de entrada donde
se elige una cartera -- transacciones, recurrentes, compras a plazo -- para
que un saldo "propio" de la cartera padre no quede escondido en vez de
sumado. Es simétrico a que un grupo de `Category` tampoco se le puede
asignar una transacción directamente.

**Reembolso: antes era un flag sin plata detrás.** El código lo dice
explícitamente (`Transaction.is_refunded`): hasta hace poco `is_refunded`
era un booleano que cualquiera podía prender/apagar sin que se moviera un
centavo. Ahora `services.register_refund` es el único lugar que lo pone: crea
una `Transaction` de INGRESO real (`refund_of` apunta al gasto original) y
recién ahí marca `is_refunded=True`. Reglas: solo se puede reembolsar un
gasto (no un ingreso ni una transferencia), solo una vez, el monto tiene que
ser mayor a 0 y no puede superar el monto original (reembolso parcial sí,
de más no). Si se borra la transacción de ingreso que reembolsaba, el gasto
original vuelve a quedar `is_refunded=False` (`perform_destroy` del
viewset) -- nunca queda desincronizado del movimiento real.

**Dos mecanismos de "dividir" independientes, que pueden coexistir.**
- `split` (por categoría): reemplaza la transacción original por N
  transacciones nuevas (soft-delete de la original), todas con la misma
  cartera/fecha, unidas por `split_group` (un UUID, no una FK). Las partes
  tienen que sumar *exactamente* el monto original. Como cada parte es una
  `Transaction` real e independiente, el saldo de la cartera y los reportes
  por categoría no necesitan ningún caso especial. Si a un `split_group` le
  queda una sola parte viva (se borraron las demás), el signal
  `_clear_split_group_if_alone` le limpia el `split_group` para que vuelva
  a comportarse como una transacción normal.
- `split-people` (por persona): NO toca monto ni categoría de la transacción
  original -- solo le agrega `paid_by` (quién puso la plata, por defecto el
  usuario autenticado vía `get_or_create_self_person`) y una
  `TransactionShare` por participante. La suma de las partes **no** tiene
  que agotar el monto total: lo que no cubren los participantes explícitos
  queda como la parte implícita del pagador, sin necesitar una fila propia.
  Llamar de nuevo con una lista distinta reemplaza el reparto anterior
  entero (borra todas las shares viejas primero).

**Compra a plazo: una sola Transaction, las cuotas son aritmética pura.**
Al crear un `InstallmentPurchase` se genera exactamente una `Transaction`
(el `total_amount`, como gasto contra `wallet`, en `start_date`) -- eso es
lo único que afecta el saldo real. Editar la compra sincroniza esa misma
transacción (monto/cartera/categoría/fecha); borrarla hace soft-delete de
ella. Las "cuotas" que ve el usuario son puro cálculo sobre
`total_amount`/`installments_total`, anclado a los cortes de facturación de
la tarjeta (`apps.accounts.services.installment_status`), no a la fecha
calendario de la compra. Por eso solo se puede crear sobre una tarjeta de
crédito con `billing_cycle_day` configurado -- ya no existe el modo "plan
de tienda" sin tarjeta. El reparto de montos (`installment_amounts`)
redondea cada cuota hacia arriba al centavo excepto la última, que es el
resto exacto, para que la suma cierre siempre con `total_amount` sin
importar el redondeo intermedio.

**Detección de recurrentes candidatos: deliberadamente conservadora.**
`detect_recurring_candidates` agrupa transacciones por (tipo, categoría,
cartera) de los últimos meses y descarta el grupo si en algún mes hubo más
de una transacción (eso ya no es "una suscripción", es gasto normal de una
categoría con mucho movimiento como "Comida"). Exige que aparezca en al
menos `min_occurrences` de los últimos `months` meses, que el monto no
varíe más del 15% (o $2, lo mayor) entre la ocurrencia más chica y la más
grande, que no exista ya un `RecurringExpense` activo para esa
categoría+cartera, y que el usuario no la haya descartado antes. Los
descartes (`RecurringSuggestionDismissal`) se identifican por monto
*aproximado* (redondeado al entero más cercano), no por transacción
puntual, para que sigan reconociendo el mismo patrón con centavos de
diferencia mes a mes.

**Generación de recurrentes es idempotente, no un contador que hay que
avanzar a mano.** `generate_recurring_transactions` avanza `next_due_date`
en el mismo bucle que crea transacciones (`while due <= as_of`), así que
correrla dos veces el mismo día (o con fechas atrasadas) no duplica nada:
genera todas las ocurrencias vencidas de una sola pasada y deja
`next_due_date` en el futuro. Cuando el usuario registra a mano una
ocurrencia desde la tarjeta "Programado" (antes de que corra el job
automático), `register_manual_recurring_occurrence` hace ese mismo avance
por separado (con `select_for_update` para no chocar con el job si corren a
la vez) -- si no existiera, `next_due_date` se quedaría igual y el job
volvería a crear la misma ocurrencia al día siguiente.

**`set-forward` de presupuestos: propaga hasta el primer valor
"personalizado".** Al fijar el presupuesto de un período, se actualiza
también cada período *futuro* que no tenía presupuesto propio o que
coincidía con el monto anterior de este -- y se corta apenas encuentra uno
que el usuario ya haya puesto en un valor distinto (eso se interpreta como
una personalización deliberada que no hay que pisar). Los períodos pasados
nunca se tocan, para conservar el histórico. Tiene un horizonte máximo por
tipo de período (`FORWARD_HORIZON_BY_PERIOD`, ~3 años de margen en cada
cadencia) para no crear filas indefinidamente si el usuario nunca vuelve a
tocar esa categoría.

**Duplicados: ventana de tolerancia de ±1 día, sin filtrar por origen.**
`find_possible_duplicates` compara mismo `wallet` + mismo `amount` dentro de
`DUPLICATE_WINDOW_DAYS` (1 día) de la fecha dada, sin excluir por `source`:
una carga manual el mismo día en que ya se importó el correo del banco
también cuenta como posible duplicado. La tolerancia de un día existe
porque algunos bancos reportan la fecha del cargo con un día de diferencia
respecto a cuándo ocurrió de verdad. Es solo informativo (`check-duplicate`):
nunca bloquea el guardado.

**Importación de Excel: una sola fuente de verdad para "qué es una
transacción válida".** `xlsx_import.py` solo traduce filas de la hoja a los
mismos campos que espera `TransactionSerializer`, o produce un `RowError`;
las reglas de negocio reales (cartera privada ajena, categoría del tipo que
no corresponde, workspace cruzado, etc.) las sigue validando ese mismo
serializer -- así no hay una segunda implementación de esas reglas que
pueda divergir. La importación es "todo lo que se pueda": cada fila se
valida por su cuenta, una fila con error no frena a las demás, y se
reportan los errores fila por fila junto con las transacciones creadas.

**El recibo (`receipt`) comparte reglas con el escaneo por IA.**
`RECEIPT_MAX_SIZE` y `RECEIPT_CONTENT_TYPES` viven en `services.py` (no en
la vista) porque el mismo archivo entra por dos puertas -- subirlo a una
transacción y mandarlo a leer por IA (`/ai/receipt/`) -- y aceptar en una lo
que la otra rechaza sería una sorpresa fea para el usuario (escanea, ve los
datos, confirma, y recién ahí falla la subida).

**Nota / algo no del todo claro:** el código de `Transaction.save()` fija
`counts_toward_budget = False` automáticamente para una transferencia
*sin* categoría, pero no dice qué pasa si luego se le agrega una categoría
a una transferencia que el usuario ya había marcado explícitamente en
`False` a mano -- no se pudo confirmar leyendo el modelo si ese `False`
manual sobrevive o se pisa (el `save()` solo actúa cuando `category_id is
None`, así que en teoría un valor explícito con categoría sí se respeta,
pero vale la pena un test explícito si no existe ya).
