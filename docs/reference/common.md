# Común (infraestructura transversal)

## Propósito

Agrupa lo que no pertenece a ningún dominio de negocio pero lo usan todos:
la base de modelos (UUID + soft-delete + multi-tenant por workspace), el
mecanismo de multi-tenancy del API (`X-Workspace-ID`), interruptores
operativos (mantenimiento, módulos apagables), utilidades de fecha
(períodos de presupuesto), checks de configuración de despliegue, y el
propio sistema de documentación que estás leyendo ahora mismo.

## Modelos principales

- **`BaseModel`**: base abstracta de (casi) todos los modelos del dominio —
  UUID como PK (facilita exportar/fusionar datos entre workspaces), soft
  delete (`is_deleted`) en vez de borrado físico, y auditoría
  `created_at`/`updated_at`. Expone `objects` (filtrado, sólo vivos) y
  `all_objects` (sin filtrar, para admin/auditoría).
- **`WorkspaceScopedModel`**: extiende `BaseModel` para cualquier modelo que
  cuelgue de un `Workspace` — evita repetir el FK sin garantía de uso
  consistente.
- **`ModuleFlag`**: interruptor manual por módulo (`key` → `is_enabled`).
  Deliberadamente **no** usa `BaseModel`/soft-delete: es un catálogo chico
  que se edita a mano desde el admin (`list_editable`), no un recurso de
  dominio con historial. Es **fail-open**: una `key` sin fila todavía se
  considera habilitada — así agregar el chequeo en un endpoint nuevo no
  exige crear la fila primero (mismo criterio que las funciones Pro de
  `apps.billing`).

## Endpoints

- `GET /api/v1/module-flags/` (`ModuleFlagsView`) — `{disabled: {key:
  mensaje}}` de los módulos apagados ahora mismo. El cliente lo consulta una
  vez para esconder/avisar una función antes de que el usuario la toque y se
  tope con un 503, en vez de enterarse recién al fallar la llamada real. Una
  `key` ausente en la respuesta está habilitada.
- `/docs/`, `/docs/<slug>/`, `/docs/changelog/` (`docs_views.py`) — este
  mismo sistema de documentación. Staff-only (`@staff_member_required`,
  misma sesión que `/admin/`, no un mecanismo nuevo). El contenido vive en
  Markdown plano en el repo (`docs/reference/*.md` y `CHANGELOG.md` en la
  raíz), no en base de datos — se actualiza con un commit normal, sin UI de
  edición. `docs_index` arma el menú lateral leyendo el directorio
  (`REFERENCE_DIR.glob("*.md")`), así que un `.md` nuevo aparece solo sin
  tocar código. El título de cada página es la primera línea `# Algo` del
  archivo. El slug se valida contra `^[a-z0-9-]+$` antes de tocar el
  filesystem.

No hay más endpoints propios de `common` — el resto de lo que ofrece
(`HasWorkspaceMembership`, `WorkspaceScopedViewSet`, etc., en `api.py`) es
infraestructura que usan los viewsets de otras apps, no rutas en sí mismas.

## Reglas de negocio y decisiones no obvias

**Multi-tenancy del API** (`api.py`): cada request opera sobre un único
workspace, identificado por el header `X-Workspace-ID` (no en la URL ni en
el JWT). `HasWorkspaceMembership.has_permission` resuelve y valida ese
header contra la membresía del usuario, dejando `request.workspace` /
`request.membership` como efecto lateral; responde 403 (no 404) si el
usuario no es miembro, a propósito, para que un usuario ajeno no pueda
distinguir "no existe" de "no tengo acceso". `WorkspaceScopedViewSet` es la
base que heredan los viewsets de dominio: filtra el queryset por ese
workspace, lo inyecta en el contexto del serializer, y hace soft-delete en
vez de `DELETE` físico.

**`AtomicOnlyForWritesMixin`**: con `ATOMIC_REQUESTS=True`, Django envuelve
*cada* request en una transacción — contra Neon, cada una es un viaje de red
extra (~25ms) que un GET de sólo lectura paga sin necesitarlo (llegó a ser
40% de las consultas en los endpoints más livianos). Este mixin saca la
transacción de los métodos seguros (GET/HEAD/OPTIONS) y la conserva para el
resto. Asume que un GET no escribe (comprobado por
`test_reads_are_read_only`).

**Middleware de mantenimiento** (`middleware.py`,
`MaintenanceModeMiddleware`): con la env var `MAINTENANCE_MODE=True`, todo el
API responde 503 con un mensaje fijo, sin tocar la base de datos — para una
ventana de mantenimiento (p. ej. una migración riesgosa) sin apagar el
servicio entero. `/healthz` y `/admin/` quedan exentos
(`MAINTENANCE_EXEMPT_PREFIXES`): el primero para que Cloud Run no mate el
servicio creyendo que se cayó, el segundo para poder seguir operando (incluso
desactivar el mantenimiento) mientras el resto está cerrado. Se
activa/desactiva redeployando con la env var cambiada — sin UI ni endpoint a
propósito, porque es un mecanismo de emergencia, no una feature de producto.

**`ModuleFlag` vs. función Pro**: son gates distintos y se usan juntos en
algunos endpoints (ver `email_import.confirm`). Una función Pro
(`apps.billing.require_feature_for_workspace`) es un límite de plan — responde
403 con mensaje de upsell. Un `ModuleFlag` apagado
(`apps.common.services.require_module_enabled`) es una decisión operativa
("algo está fallando") — responde **503**, no 403, para no ofrecer un upsell
sobre algo que no es un límite comercial sino una emergencia técnica.

**Checks de `--deploy`** (`checks.py`): tres warnings registrados con
`deploy=True`, así que **no corren en tests ni en un `manage.py` normal** —
sólo cuando `entrypoint.sh` ejecuta `manage.py check --deploy` al arrancar el
contenedor (donde sí están las variables de entorno reales), y el resultado
queda en los logs de Cloud Run:
- `common.W001`: cache por defecto en memoria de proceso con varias
  instancias de Cloud Run — el throttling de DRF (que cuenta en el cache) no
  se comparte entre instancias, así que los límites valen tantas veces como
  instancias haya y se reinician en cada deploy.
- `common.W002`: `DATABASE_URL` apunta al endpoint directo de Neon en vez del
  pooled — con `DJANGO_DB_CONN_MAX_AGE=0` se abre una conexión por request, y
  contra el endpoint directo el límite de conexiones se alcanza antes que el
  CPU en cuanto hay más de una instancia.
- `common.W003`: no hay bucket de backup configurado — sin él,
  `backup_database` no tiene dónde subir el volcado diario y lo único que
  queda es el historial de Neon (~24h en el plan free).

**`backup_database` / `run_daily_tasks`** (management commands): el volcado
diario con `pg_dump --format=custom` sube a GCS porque Neon free sólo retiene
~24h de historial — cubre "borré algo hace un rato", no "nos dimos cuenta el
lunes". Se escribe primero a un temporal y se sube después, para no dejar un
objeto truncado si `pg_dump` se cae a mitad de camino. `run_daily_tasks` es
el entrypoint de Cloud Run Job + Cloud Scheduler (no hay Celery en
producción) y corre en orden fijo: recurrentes → cierres de mes/período de
presupuesto → recordatorios → vencimientos de suscripción → backup (al
final, porque es el único paso no estrictamente idempotente: cada corrida
deja un volcado más).

**`periods.py`**: aritmética de "período de presupuesto" configurable por
workspace (diario/semanal/quincenal/mensual/anual), que reemplazó el "mes
calendario" fijo que tenía antes `CategoryBudget`. Todo período se identifica
por su fecha de inicio (`period_start`, un `date`) y nunca por índice o
año/mes sueltos, así una sola columna sirve para los cinco tipos. Las
quincenas son de calendario (1–15 y 16–fin de mes), no una ventana rodante de
14 días, para que siempre caigan dentro de un solo mes.

**`openapi.py`**: hook de postprocesado de drf-spectacular que agrega el
parámetro de header `X-Workspace-ID` (obligatorio) a toda operación bajo
`/api/v1/` salvo `auth/*`, `workspaces/*` y el webhook de email-import — para
no repetir `@extend_schema` en cada vista.

**`apps.savings` ya no existe como dominio propio**: `SavingsGoal` y
`ReserveFund` se fusionaron en `apps.accounts.Wallet` (con
`purpose=savings`) — ver `apps/savings/migrations/0002_absorb_into_wallet.py`.
La app sigue registrada únicamente para conservar el historial de
migraciones; su `models.py` está vacío. Si aparece una referencia a esos
modelos en una migración vieja o en código legado, es de antes de esa fusión
y ya no corresponde a una tabla propia.
