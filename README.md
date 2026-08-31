# buddyclone — backend

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
buddyclone/
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

cp .env.example .env               # y ajustar DJANGO_SECRET_KEY, DATABASE_URL, ...

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Celery (en otras terminales, requiere Redis):

```bash
celery -A config worker -l info
celery -A config beat -l info
```

Tareas programadas (Celery Beat):

| Tarea | Cuándo | Qué hace |
|---|---|---|
| `apps.transactions.tasks.generate_due_recurring_expenses` | diaria 00:30 | crea una `Transaction` por cada período vencido de cada `RecurringExpense` activo y adelanta `next_due_date` |
| `apps.transactions.tasks.post_due_installments` | diaria 00:35 | registra las cuotas vencidas de cada `InstallmentPurchase` e incrementa `installments_paid` |
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

`DELETE` = soft delete (`is_deleted=True`).

Esquema OpenAPI: `/api/schema/` · Swagger UI: `/api/docs/`

## Tests

```bash
python manage.py test --settings=config.test_settings
```

`config/test_settings.py` usa SQLite en memoria (no requiere PostgreSQL/Redis).
El test crítico de aislamiento multi-tenant está en
`apps/common/tests/test_workspace_isolation.py`.

## Configuración

Toda la config sensible se lee de variables de entorno o de `buddyclone/.env`
(ver `.env.example`). En producción usar `DJANGO_DEBUG=False`, que activa
HSTS, cookies seguras y redirección SSL.
