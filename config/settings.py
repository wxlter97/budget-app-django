"""
Settings de Django para el proyecto budget.

La configuración sensible / dependiente del entorno se lee de variables de
entorno (o de un archivo .env en la raíz del proyecto). Ver .env.example.
"""
import sys
from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab

RUNNING_TESTS = "test" in sys.argv

# budget/config/settings.py -> BASE_DIR = budget/
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, []),
    DJANGO_TIME_ZONE=(str, "UTC"),
    DJANGO_LANGUAGE_CODE=(str, "es"),
    MAINTENANCE_MODE=(bool, False),
    MAINTENANCE_MESSAGE=(str, "En mantenimiento por unos minutos, ya volvemos."),
    SUPPORT_WEBHOOK_URL=(str, ""),
)

# Lee budget/.env si existe (no obligatorio en producción)
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

# ---------------------------------------------------------------------------
# Núcleo
# ---------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# Ver apps.common.middleware.MaintenanceModeMiddleware / RUNBOOK.md.
MAINTENANCE_MODE = env("MAINTENANCE_MODE")
MAINTENANCE_MESSAGE = env("MAINTENANCE_MESSAGE")

# Webhook de Discord al que se reenvía cada ticket de soporte nuevo (ver
# apps.support.services.notify_new_ticket). Vacío = no se reenvía nada, el
# ticket sólo queda guardado (visible en el admin).
SUPPORT_WEBHOOK_URL = env("SUPPORT_WEBHOOK_URL")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "django_celery_beat",
    "djmoney",
    "drf_spectacular",
    "drf_spectacular_sidecar",
]

LOCAL_APPS = [
    "apps.ai",
    "apps.users",
    "apps.common",
    "apps.workspaces",
    "apps.accounts",
    "apps.transactions",
    "apps.savings",
    "apps.reports",
    "apps.loyalty",
    "apps.email_import",
    "apps.quickadd",
    "apps.notifications",
    "apps.billing",
    "apps.support",
    "apps.gamification",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.common.middleware.MaintenanceModeMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Base de datos - PostgreSQL
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://budget:budget@localhost:5432/budget",
    ),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
# Cloud Run congela la instancia entre requests: una conexión persistente puede
# quedar obsoleta. Con Neon (o cualquier Postgres serverless) poner 0.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_DB_CONN_MAX_AGE", default=60)
# Si se usa el endpoint *pooled* de Neon (PgBouncer en modo transacción) hay que
# desactivar los server-side cursors. Con el endpoint directo no hace falta.
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = env.bool(
    "DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS", default=False
)

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internacionalización
# ---------------------------------------------------------------------------
LANGUAGE_CODE = env("DJANGO_LANGUAGE_CODE")
TIME_ZONE = env("DJANGO_TIME_ZONE")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Archivos estáticos / media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# El filesystem de Cloud Run (y de cualquier contenedor) es efímero: un
# recibo guardado ahí desaparece en el próximo deploy/reinicio. Si se
# configura GS_BUCKET_NAME, los adjuntos van a Google Cloud Storage en vez
# de disco local (en dev, sin la variable, sigue usando FileSystemStorage
# de arriba — no hace falta credencial ninguna para levantar el proyecto).
# El bucket queda PRIVADO: nunca se sirve la URL de GCS directamente, los
# archivos se leen a través de `/transactions/{id}/receipt/` (ver
# apps/transactions/api.py), que ya exige la misma membresía de workspace
# que el resto del API — así no dependemos de firmar URLs de GCS.
GS_BUCKET_NAME = env("GS_BUCKET_NAME", default="")
if GS_BUCKET_NAME:
    STORAGES["default"] = {"BACKEND": "storages.backends.gcloud.GoogleCloudStorage"}
    GS_DEFAULT_ACL = None  # el bucket usa uniform bucket-level access, no ACLs por objeto
    GS_FILE_OVERWRITE = False
    GS_QUERYSTRING_AUTH = False  # nunca se expone la URL de GCS al cliente

# ---------------------------------------------------------------------------
# IA (Gemini) — ver apps/ai/
# ---------------------------------------------------------------------------
# La key vive SÓLO acá, nunca en el bundle de Expo: todo lo que es
# EXPO_PUBLIC_* queda embebido y a la vista de cualquiera que abra el bundle.
# La app le habla a nuestro backend y el backend a Gemini.
#
# **Vacía = todas las funciones de IA quedan apagadas** y el endpoint de estado
# se lo dice al front, que entonces no muestra sus entradas (mismo patrón que
# VAPID, Sentry y el botón de Google).
#
# Ojo con la capa gratis de Gemini: Google usa esos datos para entrenar. Sirve
# para probar con datos propios, nunca con datos de un usuario.
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_API_BASE = env(
    "GEMINI_API_BASE", default="https://generativelanguage.googleapis.com/v1beta"
)
# Una llamada de IA es interactiva: si tarda más que esto, al usuario le sirve
# más un error rápido y el camino manual que una pantalla colgada.
AI_TIMEOUT_SECONDS = env.int("AI_TIMEOUT_SECONDS", default=25)

# ---------------------------------------------------------------------------
# Backup de la base (manage.py backup_database, ver RUNBOOK.md)
# ---------------------------------------------------------------------------
# Neon en plan free retiene ~24 h de historial, que con usuarios de verdad no
# alcanza para recuperarse de un borrado que se note al tercer día. El volcado
# diario a GCS cubre esa ventana por centavos.
#
# Por defecto va al mismo bucket de los recibos (una variable menos que
# configurar), en el prefijo de abajo. Si preferís separarlo — por ejemplo para
# darle al bucket de backups otra política de retención o de acceso — seteá
# DB_BACKUP_BUCKET aparte.
DB_BACKUP_BUCKET = env("DB_BACKUP_BUCKET", default="") or GS_BUCKET_NAME
DB_BACKUP_PREFIX = env("DB_BACKUP_PREFIX", default="backups/db/")
# Cuántos días de volcados conservar. El comando borra los más viejos al
# terminar, pero nunca deja el prefijo vacío: si todos caducaron, el último
# sobrevive. 0 desactiva el borrado.
DB_BACKUP_RETENTION_DAYS = env.int("DB_BACKUP_RETENTION_DAYS", default=30)

# Default de Django (2.5 MB) se queda corto para una foto de recibo tomada
# con la cámara del teléfono. El límite real de tamaño lo aplica la vista
# (ver RECEIPT_MAX_SIZE en apps/transactions/api.py); este sólo evita que
# Django rechace el request antes de llegar ahí.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    "DEFAULT_THROTTLE_CLASSES": (
        ()
        if RUNNING_TESTS
        else (
            "rest_framework.throttling.AnonRateThrottle",
            "rest_framework.throttling.UserRateThrottle",
        )
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", default="40/min"),
        "user": env("THROTTLE_USER", default="1000/hour"),
        "auth": env("THROTTLE_AUTH", default="10/min"),      # login / registro
        "inbound": env("THROTTLE_INBOUND", default="120/min"),  # webhook de correo
        "quick_add": env("THROTTLE_QUICK_ADD", default="60/min"),  # Atajo de Apple Shortcuts
        "billing_webhook": env("THROTTLE_BILLING_WEBHOOK", default="120/min"),
        "dashboard": env("THROTTLE_DASHBOARD", default="30/min"),  # Villa Wxlter (saldo)
        # Cada llamada de IA cuesta plata y tarda segundos: el límite es bajo a
        # propósito. La cuota mensual del plan es el tope real (apps/ai/quotas.py);
        # esto sólo evita la ráfaga de un cliente con un bucle mal escrito.
        "ai": env("THROTTLE_AI", default="12/min"),
    },
}
if RUNNING_TESTS:
    # scopes a None => ScopedRateThrottle (login, webhook) tampoco limita
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
        key: None for key in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    }

# 60 días de refresh (antes 14, 14 sep 2026) -- "sesión de larga duración en
# este dispositivo", pedido explícito: con ROTATE_REFRESH_TOKENS +
# BLACKLIST_AFTER_ROTATION, mientras se siga abriendo la app dentro de esa
# ventana el refresh automático del cliente (ver api/client.ts) la renueva
# sola, sin volver a pedir login. El access de 30 min no cambia -- ahí no
# está el problema si el refresh funciona.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=60)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "budget API",
    "DESCRIPTION": "API REST de presupuesto personal/compartido (iOS + web).",
    "VERSION": "1.6.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "apps.common.openapi.add_workspace_id_header",
    ],
    # `LoyaltyProgram.kind` y `LoyaltyEarning.kind` comparten choices (mismo
    # concepto: puntos/cashback/descuento) pero el nombre "kind" ya lo usa
    # `Wallet.kind` (bank/credit/cash/custom, otro enum) -- sin esto,
    # drf-spectacular resuelve la colisión con un sufijo autogenerado feo.
    "ENUM_NAME_OVERRIDES": {
        "LoyaltyKindEnum": "apps.loyalty.models.LoyaltyProgram.KIND_CHOICES",
        # `RecurringExpense.type` tiene las mismas choices que
        # `Transaction.type` (income/expense/transfer) -- sin esto,
        # drf-spectacular no las funde en un solo enum y resuelve la
        # colisión de nombre con un sufijo autogenerado feo.
        "RecurringExpenseTypeEnum": "apps.transactions.models.RecurringExpense.TYPE_CHOICES",
    },
}

# ---------------------------------------------------------------------------
# Cache (backend de throttling de DRF). En prod: Redis; en dev/test: en memoria.
# ---------------------------------------------------------------------------
CACHE_URL = env("CACHE_URL", default="")
if CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL,
        }
    }
else:
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }

# ---------------------------------------------------------------------------
# CORS (front web)
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_ALL_ORIGINS = DEBUG and not CORS_ALLOWED_ORIGINS

# El front manda el workspace activo en este header; hay que permitirlo en CORS
# (los headers por defecto de django-cors-headers no incluyen los custom).
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "x-workspace-id")

# ---------------------------------------------------------------------------
# Dashboard externo (Villa Wxlter) — endpoint de solo lectura, ver apps.reports
# ---------------------------------------------------------------------------
# Token fijo compartido con el dashboard (header `Authorization: Bearer <token>`).
# Vacío (default) = el endpoint rechaza todo, nunca "abierto por accidente".
# Generar con: python -c "import secrets; print(secrets.token_urlsafe(32))"
DASHBOARD_API_TOKEN = env("DASHBOARD_API_TOKEN", default="")
# UUID del workspace a exponer. Vacío = se usa el único Workspace que exista
# (falla explícito si hay 0 o más de uno, para no mostrar el dato equivocado).
DASHBOARD_WORKSPACE_ID = env("DASHBOARD_WORKSPACE_ID", default="")

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
# Estas tareas no devuelven nada que haga falta persistir: sin result backend.
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=None)
CELERY_TASK_IGNORE_RESULT = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 5 * 60
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Orden: recurrentes -> cierres -> recordatorios (mismo orden que
# `run_daily_tasks`, el comando que reemplaza esto en producción -- ver
# DEPLOY.md §6, acá sin Celery corriendo).
CELERY_BEAT_SCHEDULE = {
    "generate-recurring-transactions": {
        "task": "apps.transactions.tasks.generate_recurring_transactions",
        "schedule": crontab(hour=0, minute=30),  # diaria
    },
    "close-previous-month": {
        "task": "apps.reports.tasks.close_previous_month",
        "schedule": crontab(hour=0, minute=5, day_of_month=1),
    },
    "close-previous-budget-period": {
        "task": "apps.reports.tasks.close_previous_budget_period",
        # Diaria, no sólo el día 1: la cadencia real (workspace.budget_period)
        # puede ser diaria o semanal, y la tarea es idempotente sola.
        "schedule": crontab(hour=0, minute=10),
    },
    "send-daily-reminders": {
        "task": "apps.notifications.tasks.send_daily_reminders",
        "schedule": crontab(hour=7, minute=0),  # a la hora en que la gente ya despertó
    },
}

# ---------------------------------------------------------------------------
# Seguridad (activa en producción, DEBUG=False)
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    # El health check de Cloud Run pega por HTTP interno (sin X-Forwarded-Proto);
    # que no se lo lleve el redirect a HTTPS.
    SECURE_REDIRECT_EXEMPT = [r"^healthz/?$"]
    SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 7)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", default="no-reply@budget.local")

# ---------------------------------------------------------------------------
# Correo saliente (invitaciones de workspace) vía Mailgun (relay SMTP).
# Sin credenciales configuradas: en DEBUG cae a la consola (no falla); en
# producción usa el backend SMTP real -- si faltan credenciales ahí, el envío
# falla ruidosamente en vez de fingir que mandó el correo.
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.console.EmailBackend"
        if DEBUG
        else "django.core.mail.backends.smtp.EmailBackend"
    ),
)
EMAIL_HOST = env("EMAIL_HOST", default="smtp.mailgun.org")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")

# Base del enlace que lleva la invitación (se le concatena "/<token>"). Un
# deep link de la app; el fallback es el esquema propio para que abra
# directo en el celular en vez de un sitio web que no existe.
INVITE_ACCEPT_URL_BASE = env("INVITE_ACCEPT_URL_BASE", default="budget://invite")

# ---------------------------------------------------------------------------
# "Continuar con Google": client ID(s) OAuth contra los que se valida el
# id_token (uno por plataforma -- iOS, Android, web -- todos apuntan a la
# misma cuenta de Google Cloud). Vacío = se acepta cualquier audiencia (solo
# para desarrollo local sin credenciales reales todavía).
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_IDS = [c for c in env.list("GOOGLE_CLIENT_IDS", default=[]) if c]

# ---------------------------------------------------------------------------
# Push notifications en la versión web (Web Push / RFC 8291, vía VAPID).
# Vacío = se omiten los pushes a navegadores (nativo sigue andando igual,
# ese va por Expo). Generar un par con:
#   python manage.py generate_vapid_keys
# ---------------------------------------------------------------------------
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_SUBJECT = env("VAPID_SUBJECT", default="mailto:soporte@budget.local")

# ---------------------------------------------------------------------------
# Importación por correo bancario (webhook de correo entrante)
# ---------------------------------------------------------------------------
INBOUND_EMAIL_LOCALPART = env("INBOUND_EMAIL_LOCALPART", default="import")
INBOUND_EMAIL_DOMAIN = env("INBOUND_EMAIL_DOMAIN", default="inbound.budget.local")
# Secreto compartido que debe traer el webhook en el header X-Inbound-Secret.
# Vacío = el endpoint rechaza todo (fail-closed).
INBOUND_WEBHOOK_SECRET = env("INBOUND_WEBHOOK_SECRET", default="")
# Si se configura, se verifica la firma HMAC nativa de Mailgun cuando el
# payload trae timestamp/token/signature (en vez del secreto en el header).
INBOUND_MAILGUN_SIGNING_KEY = env("INBOUND_MAILGUN_SIGNING_KEY", default="")

# ---------------------------------------------------------------------------
# Pagos y suscripciones (apps.billing) -- ver apps/billing/providers.py
# ---------------------------------------------------------------------------
# Proveedor que usa el checkout cuando el cliente no especifica uno.
# Agregar un proveedor nuevo = una clase en providers.py + este nombre.
DEFAULT_PAYMENT_PROVIDER = env("DEFAULT_PAYMENT_PROVIDER", default="wompi")

WOMPI_API_KEY = env("WOMPI_API_KEY", default="")
WOMPI_WEBHOOK_SECRET = env("WOMPI_WEBHOOK_SECRET", default="")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}

# ---------------------------------------------------------------------------
# Error tracking (Sentry) -- opcional. Vacío = no hace nada (ni importa el
# SDK), así que no hace falta tener la cuenta creada para que el resto de la
# app funcione. Cuando exista un proyecto de Sentry, sólo hay que setear
# SENTRY_DSN (Secret Manager en Cloud Run, ver DEPLOY.md) -- nada de código
# que tocar de nuevo.
# ---------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN and not RUNNING_TESTS:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", default="production" if not DEBUG else "development"),
        release=env("SENTRY_RELEASE", default=None),
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            # Cualquier `logger.error(...)` (no sólo excepciones no manejadas)
            # también viaja a Sentry como evento -- útil para los `except`
            # que ya loguean en vez de re-lanzar (p. ej. `webPush`/providers).
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        # Traza de performance: 10% de las requests alcanza para ver
        # tendencias sin acercarse a los límites del plan free de Sentry.
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        send_default_pii=False,
    )
