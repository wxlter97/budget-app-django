"""Settings para la test suite.

    python manage.py test --settings=config.test_settings

Usa SQLite en memoria salvo que haya DATABASE_URL en el entorno (CI corre
contra PostgreSQL). El throttling de DRF se desactiva solo cuando "test"
está en sys.argv (ver config/settings.py).
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = ["*"]
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

if not os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# Password hasher rápido para tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
