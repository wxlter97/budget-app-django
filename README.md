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
└── apps/
    ├── users/         # AUTH_USER_MODEL personalizado (users.User)
    ├── common/        # BaseModel: UUID PK, soft delete, auditoría, scoping
    ├── workspaces/    # Workspace (presupuesto compartido) + Membership
    ├── accounts/      # Account, Asset, Liability, Debt
    ├── transactions/  # Category, Transaction, CategoryBudget, ...
    ├── savings/       # SavingsGoal, ReserveFund
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

## Saldos de cuenta

`Account.opening_balance` es el punto de partida fijo (lo fija el cliente al
crear la cuenta). `Account.current_balance` es un valor **cacheado** =
`opening_balance + Σ transacciones vivas` (income suma, expense resta). Lo
mantienen signals sobre `Transaction` (alta, edición de monto/categoría/cuenta,
soft y hard delete). Para reconciliar:

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
| `/accounts/` · `/assets/` | Scoped. Los `private` solo los ve/usa su `owner`. |
| `/liabilities/` · `/debts/` | Scoped. |
| `/categories/` | Scoped. `parent` debe ser del mismo workspace. |
| `/transactions/` | Scoped vía `account.workspace`. Valida que `account` y `category` sean del workspace. |
| `/category-budgets/` | Scoped. Único por `(category, month, year)`. |
| `/recurring-expenses/` · `/installment-purchases/` | Scoped. Validan `account` y `category` del workspace. |
| `/savings-goals/` · `/reserve-funds/` | Scoped. |
| `/monthly-snapshots/` | Scoped, **solo lectura** (los genera la tarea de cierre de mes). |
| `/bank-email-schemas/` | Config global. Lectura: cualquier autenticado (solo `is_active`). Escritura: solo staff. |
| `/email-import-logs/` | Scoped, solo lectura + `?status=`. Acciones: `POST .../{id}/confirm/` (body: `category` obligatorio; `account`/`amount`/`date`/`description` opcionales, caen a los valores extraídos del correo — crea la `Transaction`) y `POST .../{id}/reject/`. Solo sobre logs en estado `pending`. |
| `POST /email-import/inbound/` | **Webhook** de correo entrante. Auth: header `X-Inbound-Secret: <INBOUND_WEBHOOK_SECRET>` **o** firma HMAC nativa de Mailgun si se configura `INBOUND_MAILGUN_SIGNING_KEY`. Body JSON/form: `{to, from, subject, text}` (también acepta los nombres de Mailgun/SendGrid/Postmark). Responde `202 {log_id, status}`. |
| `POST /workspaces/{id}/rotate-inbound-token/` | Rota el token de importación (solo owner). |

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
   no. Si el parser extrae los últimos 4 dígitos, intenta matchear la `Account`.
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
| `GET /reports/net-worth/` | desglose del patrimonio neto (cuentas, activos, pasivos, deudas, neto) |
| `GET /reports/cashflow/?months=` | serie mensual de ingresos/gastos/neto (default 6, máx 24) |
| `GET /reports/summary/` | resumen del dashboard: mes actual, patrimonio, importaciones pendientes, top 5 categorías de gasto |

Las cuentas/activos `private` de los que el usuario no es `owner` quedan
fuera de todos los reportes (igual que en `/accounts/` y `/transactions/`).

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
