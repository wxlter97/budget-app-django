# budget — backend

Backend Django + API REST para una app de presupuesto personal/compartido,
consumida por una app iOS nativa y una interfaz web (mismo API `/api/v1/`).

## Stack

- Django 5.2 (LTS) + Django REST Framework
- PostgreSQL
- Autenticación JWT (`djangorestframework-simplejwt`, con blacklist de refresh)
- Celery + django-celery-beat (Redis como broker) para cierre de mes, gastos
  recurrentes e importación de correos bancarios
- django-money / numpy-financial para montos y cálculos financieros
- drf-spectacular para el esquema OpenAPI (`/api/schema/`, `/api/docs/`)

## Estructura

```
budget/
├── manage.py
├── requirements.txt
├── .env.example
├── config/            # proyecto Django (settings, urls, wsgi/asgi, celery)
├── infra/             # config de infraestructura versionada (ciclo de vida del bucket)
└── apps/
    ├── ai/            # Gemini: cliente, cuota mensual por plan y log de consumo
    ├── users/         # AUTH_USER_MODEL personalizado (users.User)
    ├── common/        # BaseModel: UUID PK, soft delete, auditoría, scoping
    ├── workspaces/    # Workspace (presupuesto compartido) + Membership
    ├── accounts/      # Wallet (cartera: gasto / ahorro / deuda / activo)
    ├── transactions/  # Category, Transaction, CategoryBudget, ...
    ├── savings/       # (vacía; fusionada en accounts.Wallet)
    ├── reports/       # MonthlySnapshot
    └── email_import/  # BankEmailSchema, EmailImportLog
```

## Puesta en marcha (desarrollo)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # y ajustar DATABASE_URL, etc.
# generar el secret key (url-safe: no rompe el parser de .env de docker compose):
python -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))"

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Celery (en otras terminales, requiere Redis):

```bash
celery -A config worker -l info
celery -A config beat -l info
```

## Docker

```bash
docker compose up --build
```

Levanta `db` (PostgreSQL 17), `redis`, `web` (gunicorn en `:8000`, corre
`migrate` al arrancar), `worker` y `beat`. Variables desde el entorno o un
`.env` (ver `docker-compose.yml`). Healthcheck en `GET /healthz/`.

Tareas programadas (Celery Beat):

| Tarea | Cuándo | Qué hace |
|---|---|---|
| `apps.transactions.tasks.generate_recurring_transactions` | diaria 00:30 | crea una `Transaction` (`source=recurring`) por cada período vencido de cada `RecurringExpense` activo y adelanta `next_due_date` |
| `apps.transactions.tasks.post_due_installments` | diaria 00:35 | registra las cuotas vencidas de cada `InstallmentPurchase` (`source=installment`) e incrementa `installments_paid` |
| `apps.reports.tasks.close_previous_month` | día 1, 00:05 | genera el `MonthlySnapshot` del mes anterior (por workspace) y hace el rollover del sobrante de cada categoría a su `CategoryProvision` |

## Carteras (`Wallet`) y saldos

Una `Wallet` (cartera) unifica cuentas, ahorros, activos y deudas. Campos clave:
- `purpose` = `spending` | `savings` | `debt` | `asset`.
- `counts_toward_net_worth` (bool): si es `False` la cartera no suma al
  patrimonio neto (sí aparece en los totales por tipo).
- `parent` (self-FK): sub-carteras. `aggregated_balance` = saldo propio + el de
  los descendientes (solo presentacional; el neto suma los `current_balance`
  propios para no doble-contar).
- Ahorro: `goal_amount`, `goal_date`, `monthly_contribution`, `progress_pct`.

`opening_balance` es el punto de partida fijo. `current_balance` (propio) es un
valor **cacheado** = `opening_balance + Σ transacciones vivas` (income +,
expense −, transfer saliente −, entrante +). Lo mantienen signals sobre
`Transaction`. Las carteras sin movimientos (activo, préstamo, bucket de ahorro)
se ajustan editando `opening_balance` (el serializer recalcula `current_balance`).
Para reconciliar:

```bash
python manage.py recompute_balances
```

## API v1

Base: `/api/v1/`. Autenticación: `Authorization: Bearer <access token>`.

| Auth | |
|---|---|
| `POST /auth/register/` | `{username, email, password, first_name?, last_name?}` → crea la cuenta y devuelve `{user, access, refresh}` |
| `POST /auth/token/` | `{username, password}` → `{access, refresh}` |
| `POST /auth/token/refresh/` | `{refresh}` → `{access}` |
| `GET/PATCH /auth/me/` | usuario autenticado (el `username` es de solo lectura) |

**Selección de workspace:** salvo `/workspaces/`, todos los endpoints exigen el
header `X-Workspace-ID: <uuid>`. Un permission valida que el usuario sea miembro
de ese workspace; si no lo es responde `403` (no `404`, para no revelar si el
workspace existe). El queryset de cada viewset se filtra por ese workspace, así
que los objetos de otros workspaces devuelven `404` aunque conozcas el UUID.

| Endpoint | Notas |
|---|---|
| `GET/POST /workspaces/` | No usa el header. `POST` crea el workspace + Membership owner. `PATCH/DELETE` solo owner. |
| `/memberships/` | Scoped por header. Lectura: cualquier miembro. Alta (`{"email": "..."}`) / cambio de rol / expulsión: solo owner. Protege al último owner. |
| `/wallets/` | Scoped. Carteras (gasto/ahorro/deuda/activo). Las `private` solo las ve/usa su `owner`. Filtros: `purpose`, `parent`, `is_active`, `counts_toward_net_worth`. |
| `/categories/` | Scoped. `parent` debe ser del mismo workspace. |
| `/transactions/` | Scoped vía `wallet.workspace`. `type` = income/expense/transfer; en transfer manda `wallet` + `to_wallet` sin categoría. |
| `/category-budgets/` | Scoped. Único por `(category, month, year)`. |
| `/recurring-expenses/` · `/installment-purchases/` | Scoped. Validan `wallet` y `category` del workspace. |
| `/monthly-snapshots/` | Scoped, **solo lectura** (los genera la tarea de cierre de mes). |
| `/bank-email-schemas/` | Config global. Lectura: cualquier autenticado (solo `is_active`). Escritura: solo staff. |
| `/email-import-logs/` | Scoped, solo lectura + `?status=`. Acciones: `POST .../{id}/confirm/` (body: `category` obligatorio; `wallet`/`amount`/`date`/`description` opcionales, caen a los valores extraídos del correo — crea la `Transaction`) y `POST .../{id}/reject/`. Solo sobre logs en estado `pending`. |
| `POST /email-import/inbound/` | **Webhook** de correo entrante. Auth: header `X-Inbound-Secret: <INBOUND_WEBHOOK_SECRET>` **o** firma HMAC nativa de Mailgun si se configura `INBOUND_MAILGUN_SIGNING_KEY`. Body JSON/form: `{to, from, subject, text}` (también acepta los nombres de Mailgun/SendGrid/Postmark). Responde `202 {log_id, status}`. |
| `POST /workspaces/{id}/rotate-inbound-token/` | Rota el token de importación (solo owner). |
| `GET /ai/status/` | No usa el header (la cuota es por usuario). `{enabled, quotas: {receipt, parse, chat}, resets_at}` — `enabled: false` cuando no hay `GEMINI_API_KEY`. Ver `apps/ai/`. |
| `POST /ai/receipt/` | Scoped. `multipart` con `file` (JPG/PNG/WEBP/HEIC/PDF, ≤8 MB) y `wallet` opcional. Devuelve una **candidata** editable con confianza por campo y posibles duplicados. **No crea la transacción ni guarda el archivo.** 429 si se acabó la cuota del plan, 503 si Gemini no contestó. |
| `POST /ai/parse/` | Scoped. `{text, wallet?}` — una frase suelta ("gasté 12.50 en almuerzo con la tarjeta") a la misma **candidata**, más el tipo y la cartera si la frase los nombra. Tampoco crea nada. Cuota, 429 y 503 iguales. |

### Importación por correo — cómo funciona

1. Cada workspace tiene un `inbound_token` y una dirección
   `import+<token>@<INBOUND_EMAIL_DOMAIN>` (campo `inbound_email` en el API).
2. El usuario configura una regla en su correo para **reenviar** las
   notificaciones del banco a esa dirección.
3. El proveedor de correo entrante (SendGrid Inbound Parse / Mailgun Routes /
   Postmark) hace `POST` a `/api/v1/email-import/inbound/` con el
   `X-Inbound-Secret`.
4. `ingest_inbound_email` resuelve el workspace por el token, matchea un
   `BankEmailSchema` activo por `sender_pattern` (regex sobre el remitente),
   corre el parser de ese banco (`apps/email_import/bank_parsers/<slug>.py`,
   registrado con `@register("<slug-de-bank_name>")`) y crea un
   `EmailImportLog` — `pending` si todo salió bien, `failed` con el motivo si
   no. Si el parser extrae los últimos 4 dígitos, intenta matchear la cartera (`Wallet`) por `card_last4`.
5. El usuario revisa `/email-import-logs/?status=pending` y confirma/rechaza.
   **Nunca se crea una `Transaction` automáticamente.**

Agregar un banco = crear un `BankEmailSchema` (`bank_name`, `sender_pattern`)
+ un módulo en `bank_parsers/` cuyo `@register("<slug>")` coincida con
`slugify(bank_name)`. Parsers de ejemplo: `demo_bank.py`, `nu_style.py`.

`DELETE` = soft delete (`is_deleted=True`).

### Reportes (solo lectura, scoped por header)

| Endpoint | |
|---|---|
| `GET /reports/budget/?year=&month=` | presupuesto vs. gasto real por categoría + totales (default: mes actual) |
| `GET /reports/net-worth/` | `{ net, by_purpose: {spending, savings, debt, asset} }` |
| `GET /reports/cashflow/?months=` | serie mensual de ingresos/gastos/neto (default 6, máx 24) |
| `GET /reports/summary/` | resumen del dashboard: mes actual, patrimonio, importaciones pendientes, top 5 categorías de gasto |

Las cuentas/activos `private` de los que el usuario no es `owner` quedan
fuera de todos los reportes (igual que en `/wallets/` y `/transactions/`).

Esquema OpenAPI: `/api/schema/` · Swagger UI: `/api/docs/` · Redoc: `/api/redoc/`
(Swagger/Redoc se sirven **sin CDN** vía `drf-spectacular-sidecar`).

El header `X-Workspace-ID` aparece documentado automáticamente en todas las
operaciones que lo requieren (hook `apps/common/openapi.py`).

### Rate limiting

`AnonRateThrottle` + `UserRateThrottle` globales, más scopes propios para
login/registro y el webhook. Rates configurables por entorno
(`THROTTLE_ANON`, `THROTTLE_USER`, `THROTTLE_AUTH`, `THROTTLE_INBOUND`).
Backend: `CACHE_URL` (Redis) en producción, en memoria si no se define.
Desactivado automáticamente durante los tests.

## IA (Gemini)

`apps/ai` es el único lugar que conoce `GEMINI_API_KEY` y el único que habla
con Google; la app nunca le pega directo (todo lo `EXPO_PUBLIC_*` queda
embebido en el bundle). **Sin la key, la IA queda apagada entera** y el front
no muestra sus entradas.

Todo pasa por `services.run()`, que hace siempre lo mismo y en este orden:

1. **Chequea la cuota** (`quotas.check`) antes de gastar la llamada. Los topes
   mensuales viven en `Plan.features` (`ai_receipts_per_month`,
   `ai_parses_per_month`, `ai_chats_per_month`), así que se ajustan desde
   `/admin/` sin deploy. A diferencia del resto de los feature flags de
   `apps.billing`, esto es **fail-closed**: un plan sin esas claves aplica los
   números del gratis — acá el costo de equivocarse es una factura.
2. **Llama a Gemini** (`client.generate`), con un solo reintento y traduciendo
   cualquier fallo a `AIUnavailable`. Que la IA se caiga nunca debe tumbar
   nada: el alta manual de una transacción tiene que seguir andando.
3. **Registra el consumo** en `AIUsage` — operación, modelo, tokens, costo
   estimado y latencia, **sin nada de lo que el usuario escribió ni de lo que
   la IA respondió**. Esa misma tabla es el contador de la cuota, así que no
   hay dos fuentes que puedan discrepar. Una llamada que falla del lado de
   Google se registra pero no le come la cuota al usuario.

Qué modelo atiende cada operación y cuánto cuesta: `apps/ai/pricing.py`.

### Las dos entradas: recibo y frase

`POST /ai/receipt/` y `POST /ai/parse/` devuelven **la misma forma de
candidata** y con el mismo contrato (editable, confianza por campo, nada
guardado). Es a propósito: el cliente las muestra con la misma pantalla, y los
canales que vienen después (Telegram, voz) entran por `/ai/parse/` sin inventar
un formato nuevo.

Lo común de "no creerle al modelo" vive en `apps/ai/normalize.py` y lo usan las
dos: un monto que no parsea, negativo o absurdo queda vacío; una fecha futura o
de hace más de dos años cae a hoy; el modelo no puede declararse seguro de un
campo que no se pudo usar.

#### Escaneo de recibos

`POST /ai/receipt/` (ver `apps/ai/receipts.py`) devuelve una **candidata**, nunca
una transacción: el usuario siempre confirma, y el archivo se guarda como
`Transaction.receipt` recién cuando aprieta guardar — un escaneo descartado no
deja nada en el bucket.

Dos cosas que ordenan ese archivo:

- **La categoría se resuelve primero con el historial** del workspace
  (`guess_category_by_merchant`: gratis, determinista, y sabe cómo categorizó
  *esta* gente *este* comercio antes) y sólo después con la sugerencia del
  modelo, que además tiene que matchear una categoría **asignable** — sugerir
  un grupo daría una transacción que no se puede guardar.
- **Nada de lo que devuelve el modelo se cree sin normalizar.** Un monto que no
  parsea, negativo o absurdo queda vacío y marcado `low` en vez de inventado; una
  fecha futura, o de hace más de dos años (el año mal leído de un ticket térmico),
  cae a hoy. Vale más un campo vacío que el usuario llena que uno inventado que
  no mira.

#### Texto libre

`POST /ai/parse/` (ver `apps/ai/parsing.py`) resuelve además dos cosas que un
recibo no tiene: el **tipo** ("me pagaron 800" es un ingreso) y la **cartera**
("con la tarjeta").

Al modelo se le pasan los **nombres reales** de las carteras y categorías del
workspace, y se le pide que elija uno de esa lista. Sin eso, "con la tarjeta"
vuelve como texto libre que después hay que adivinar a qué fila corresponde, y
se falla seguido. Cuesta unos 150 tokens de entrada — en Flash-Lite,
centésimas de centavo — y sale mucho más barato que una categoría mal puesta.

Dos consecuencias de ese diseño:

- Las carteras **privadas** de las que el usuario no es dueño no entran al
  prompt. Que el modelo las viera ya sería filtrarlas, aunque no las devolviera.
- Lo que el modelo responde se matchea contra **esa misma lista**, nunca contra
  la base de nuevo, así que no hay forma de que resuelva algo que el usuario no
  podía elegir.

## Tests

```bash
python manage.py test --settings=config.test_settings
```

`config/test_settings.py` usa SQLite en memoria (no requiere PostgreSQL/Redis).
El test crítico de aislamiento multi-tenant está en
`apps/common/tests/test_workspace_isolation.py`.

## Configuración

Toda la config sensible se lee de variables de entorno o de `budget/.env`
(ver `.env.example`). En producción usar `DJANGO_DEBUG=False`, que activa
HSTS, cookies seguras y redirección SSL.

Qué funciones ya están programadas pero no hacen nada hasta que se les
configure una key, un DNS o unas filas en `/admin/`: ver `CONFIG-PENDIENTE.md`.

Qué aguanta el stack en producción y cuánto cuesta por usuario:
ver `COSTOS-Y-ESCALA.md`.

**Qué sigue y en qué orden (índice maestro): `ROADMAP.md`.**
