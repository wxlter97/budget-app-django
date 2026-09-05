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
    "apps.users",
    "apps.common",
    "apps.workspaces",
    "apps.accounts",
    "apps.transactions",
    "apps.savings",
    "apps.reports",
    "apps.email_import",
    "apps.quickadd",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
    },
}
if RUNNING_TESTS:
    # scopes a None => ScopedRateThrottle (login, webhook) tampoco limita
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
        key: None for key in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    }

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=14)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "budget API",
    "DESCRIPTION": "API REST de presupuesto personal/compartido (iOS + web).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "apps.common.openapi.add_workspace_id_header",
    ],
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

# Orden en el día 1: recurrentes -> cuotas -> cierre de mes.
CELERY_BEAT_SCHEDULE = {
    "generate-recurring-transactions": {
        "task": "apps.transactions.tasks.generate_recurring_transactions",
        "schedule": crontab(hour=0, minute=30),  # diaria
    },
    "post-due-installments": {
        "task": "apps.transactions.tasks.post_due_installments",
        "schedule": crontab(hour=0, minute=35),
    },
    "close-previous-month": {
        "task": "apps.reports.tasks.close_previous_month",
        "schedule": crontab(hour=0, minute=5, day_of_month=1),
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

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}
