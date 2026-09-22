# Workspaces

## Propósito

Un `Workspace` es el "presupuesto" tal como lo ve el usuario en la app (se
llama así en el código, no `Budget`, para no chocar con `CategoryBudget`, que
es el monto presupuestado por categoría/mes). Resuelve el caso de un
presupuesto compartido por varias personas (pareja, familia, roommates):
varios usuarios viendo y editando las mismas carteras, categorías y
transacciones, con un rol de `owner` que controla lo destructivo. También
resuelve el caso de carteras en más de una moneda dentro del mismo workspace,
vía tasas de cambio manuales.

Todo el resto del dominio (carteras, categorías, transacciones, presupuestos,
recurrentes...) cuelga de un `Workspace`; casi todos los endpoints "de
dominio" del API llevan un header `X-Workspace-ID` que se resuelve a través
de `HasWorkspaceMembership` (ver `apps/common/api.py`).

## Modelos principales

- **`Workspace`**: nombre, `base_currency` (moneda en la que se expresan los
  totales agregados: patrimonio neto, presupuesto, flujo de caja),
  `inbound_token` (identifica al workspace en la dirección de importación por
  correo `import+<token>@<dominio>`, rotable si se filtra) y `budget_period`
  (cadencia de `CategoryBudget`: mensual, semanal, etc. -- una sola
  preferencia por workspace, no por categoría). `budget_period_closed_through`
  guarda hasta qué período ya se le hizo rollover de provisión (ver
  `apps.reports.services.close_previous_budget_period`); se resetea a `None`
  cuando cambia `budget_period`, para no arrastrar un cálculo hecho bajo la
  grilla de períodos vieja.
- **`Membership`**: usuario + workspace + `role` (`owner` / `member`).
  `unique_membership_per_workspace` impide duplicados. No hay borrado
  duro normal (`soft_delete`), y siempre tiene que quedar al menos un
  `owner` (ver reglas de negocio).
- **`Invitation`**: invitación por correo a alguien que **todavía no tiene
  cuenta** (si ya la tiene, se agrega directo como `Membership`, sin pasar
  por acá). Trae `token` único, `status` (`pending` / `accepted` /
  `declined`) y `role` (el que va a tener la `Membership` una vez aceptada).
  Sólo puede existir una invitación `pending` por (workspace, email)
  simultánea.
- **`ExchangeRate`**: tasa manual `1 <currency> = <rate_to_base>
  <workspace.base_currency>`, cargada a mano por el usuario (no hay API
  externa de cotizaciones). Única por (workspace, currency).

## Endpoints

Todos bajo `/api/v1/`, montados por el router (`config/api_router.py`):
`workspaces/`, `memberships/`, `invitations/`, `exchange-rates/`.

**Workspace** (`WorkspaceViewSet`, CRUD sin header `X-Workspace-ID`: la
pertenencia se deduce de las `Membership` del usuario autenticado, no del
header):
- CRUD estándar. Lectura: cualquier miembro. Escritura/borrado: sólo `owner`
  (`WorkspaceOwnerOrReadOnly`). Crear un workspace lo siembra con el set de
  categorías por defecto (`seed_default_categories`) y crea al creador como
  `owner`; está limitado por el plan (`can_own_another_workspace`). No se
  puede borrar el único workspace del usuario (el cliente asume que siempre
  hay uno activo).
- `POST /{id}/rotate-inbound-token/`: nueva dirección de importación por
  correo, invalida la anterior. Sólo owner.
- `POST /{id}/reset/`: borra datos del workspace (`scope=movimientos` o
  `todo`), irreversible, requiere `confirm: true`. Sólo owner.
- `GET /{id}/backup/`: exporta todo el contenido del workspace a JSON
  (conserva los UUID originales de cada fila para poder reconstruir
  relaciones al restaurar). Requiere feature `backup` del plan. Sólo owner.
- `POST /{id}/restore/`: reemplaza TODO el contenido del workspace por un
  backup (borra como `reset scope=todo` y repuebla). Irreversible, requiere
  `confirm: true` y feature `backup`. Sólo owner.

**Membership** (`MembershipViewSet`, con header `X-Workspace-ID`):
- Lectura: cualquier miembro. Alta / cambio de rol / expulsión: sólo owner
  (`IsWorkspaceOwner`).
- `POST` (crear): si el email ya tiene cuenta, agrega la `Membership`
  directo (201). Si no, crea/reutiliza una `Invitation` pendiente y manda el
  correo de invitación (202) -- el código de estado es lo que el cliente usa
  para distinguir "ya quedó adentro" de "le mandamos un correo". Limitado por
  el plan (`can_add_member`).
- `DELETE` (expulsar) y `PATCH` (cambiar rol): no se puede dejar al workspace
  sin ningún `owner`.

**Invitation** (`InvitationViewSet`, lookup por `token`, sin header --
resueltas por el email del usuario autenticado):
- `list`/`retrieve`: `list` trae las invitaciones pendientes del usuario
  autenticado (por su email). `retrieve` es público (`AllowAny`): así el
  enlace del correo se puede abrir sin sesión iniciada, para mostrar a qué
  te invitaron antes de loguearse/registrarse.
- `POST /{token}/accept/`: crea la `Membership` (con el rol de la
  invitación) y marca la invitación `accepted`. Valida que el email de la
  invitación coincida con el del usuario logueado, y el límite de miembros
  del plan del workspace destino.
- `POST /{token}/decline/`: marca `declined`.
- Ambas acciones resuelven (`resolve_notifications`) la notificación de
  `Notification.KIND_INVITATION` asociada, para que deje de pedir acción en
  el centro de notificaciones.

**ExchangeRate** (`ExchangeRateViewSet`, con header `X-Workspace-ID`):
- CRUD. Cualquier miembro puede cargar/editar tasas (no se restringe a owner:
  no es destructivo). `create` hace upsert por (workspace, currency): cargar
  una tasa para una moneda que ya tenía una la actualiza, en vez de
  rechazar por duplicado. Requiere feature `multi_currency` del plan para
  crear. No se puede cargar una tasa para la propia `base_currency` del
  workspace. El borrado es duro (no soft-delete): si fuera soft, la
  constraint de unicidad (workspace, currency) seguiría "ocupada" y no se
  podría volver a cargar una tasa para esa moneda.

## Reglas de negocio y decisiones no obvias

- **Conversión de moneda sin API externa**: `apps.workspaces.currency.
  get_rate_map(workspace)` arma `{moneda: tasa_a_base}` a partir de los
  `ExchangeRate` cargados, con la propia `base_currency` del workspace en
  1. `convert(amount, currency, rate_map)` devuelve `None` si la moneda no
  es la base y no tiene tasa configurada -- **el llamador decide qué hacer**,
  y en todos los reportes (`apps.reports.services`) el criterio es "se
  excluye del total silenciosamente". Esto significa que una cartera en una
  moneda sin tasa cargada simplemente no aparece en patrimonio neto,
  presupuesto ni flujo de caja, sin ningún error visible -- si un total
  agregado "no cierra", lo primero a revisar es si falta una `ExchangeRate`.
- **Owner vs member**: el único gate real de rol es "destructivo/estructural
  vs. no". Owner: borrar/resetear/restaurar el workspace, rotar el token de
  importación, invitar/expulsar/cambiar roles de miembros, backup/restore.
  Member: todo lo operativo (transacciones, presupuestos, tasas de cambio,
  etc.) sin distinción de rol. Nunca puede quedar un workspace sin ningún
  owner (ver `MembershipSerializer._is_last_owner`, chequeado tanto al
  cambiar rol como al expulsar).
- **Invitación vs alta directa**: `MembershipViewSet.create` decide en el
  momento si el email ya tiene cuenta. Esto evita que invitar a alguien sin
  cuenta todavía sea un error de "usuario no encontrado" -- en cambio genera
  una `Invitation` (reutilizando la pendiente existente si ya había una, para
  que "reinvitar" reenvíe el mismo enlace en vez de crear uno nuevo) y manda
  el correo. El código de respuesta (201 vs 202) es el contrato con el
  cliente para distinguir ambos casos.
- **`budget_period_closed_through` se resetea al cambiar `budget_period`**:
  cambiar la cadencia de presupuesto no reescribe los `CategoryBudget.
  period_start` ya guardados (siguen representando el período que
  representaban al crearse), pero si se dejara el rollover marcado bajo la
  fecha vieja, el próximo cierre automático (`close_previous_budget_period`)
  trataría de ponerse al día período por período bajo la grilla NUEVA desde
  una fecha pensada para la vieja -- casi seguro desalineado. Por eso
  `WorkspaceSerializer.update` arranca ese contador de cero en vez de
  intentar reconstruir el rollover retroactivo cruzando el cambio de
  cadencia.
- **Backup/restore preserva UUIDs**: `export_backup` conserva el UUID
  original de cada fila (carteras, categorías, transacciones, etc.) para que
  las relaciones se reconstruyan tal cual al restaurar. La consecuencia es
  que un mismo backup **no se puede restaurar dos veces en workspaces que
  coexisten** con esos datos aún presentes (los IDs son globales por tabla,
  no por workspace) -- `_check_no_id_collisions` lo rechaza con un mensaje
  claro antes de tocar la base. El saldo de cada cartera nunca se copia del
  backup: se recalcula agregando las transacciones ya restauradas, una
  pasada por cartera.
- **`Wallet.visibility`** (privada vs compartida) no es un concepto de
  `Membership`/rol: es un flag en la cartera, filtrado en los reportes por
  `owner_id` del usuario que consulta (ver `_visible`/`_wallet_ok` en
  `apps.reports.services`). Esto se documenta en `reports.md`, pero conviene
  tenerlo presente acá: dos miembros del mismo workspace pueden ver montos
  agregados distintos si hay carteras privadas de por medio.
