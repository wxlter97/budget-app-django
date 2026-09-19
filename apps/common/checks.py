"""
Avisos de configuración que sólo importan en producción y que, sin esto, no
se notan hasta que hacen daño en silencio.

Son checks de `--deploy`, así que no corren en los tests ni en un `manage.py`
normal: los dispara `entrypoint.sh` con `manage.py check --deploy` al arrancar
el contenedor, donde sí están las variables de entorno reales, y el resultado
queda en los logs de Cloud Run.
"""
from django.conf import settings
from django.core.checks import Tags, Warning, register

_LOCMEM = "django.core.cache.backends.locmem.LocMemCache"


@register(Tags.caches, deploy=True)
def cache_is_shared_between_instances(app_configs, **kwargs):
    """El throttling de DRF cuenta en el cache. Con cache en memoria y varias
    instancias de Cloud Run, cada una lleva su propia cuenta: los límites
    valen tantas veces como instancias haya, y se reinician en cada deploy."""
    if settings.DEBUG:
        return []
    if settings.CACHES.get("default", {}).get("BACKEND") != _LOCMEM:
        return []
    return [
        Warning(
            "El cache por defecto es en memoria del proceso, así que el "
            "throttling de DRF no se comparte entre instancias.",
            hint="Definí CACHE_URL apuntando a un Redis (ver CONFIG-PENDIENTE.md).",
            id="common.W001",
        )
    ]


@register(Tags.database, deploy=True)
def neon_uses_pooled_endpoint(app_configs, **kwargs):
    """Con DJANGO_DB_CONN_MAX_AGE=0 se abre una conexión por request. Contra el
    endpoint directo de Neon, el límite de conexiones se alcanza antes que el
    CPU en cuanto hay más de una instancia sirviendo."""
    if settings.DEBUG:
        return []
    host = settings.DATABASES.get("default", {}).get("HOST") or ""
    if "neon.tech" not in host or "-pooler" in host:
        return []
    return [
        Warning(
            f"DATABASE_URL apunta al endpoint directo de Neon ({host}), no al pooled.",
            hint="Usá el host con '-pooler' y DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True "
                 "(ver DEPLOY.md §1).",
            id="common.W002",
        )
    ]


@register(Tags.database, deploy=True)
def database_backups_have_a_bucket(app_configs, **kwargs):
    """Sin bucket, `backup_database` vuelca y tira el archivo, así que lo único
    que queda es el historial de Neon: ~24 h en el plan free. Un borrado que se
    note el lunes ya no tiene de dónde recuperarse."""
    if settings.DEBUG:
        return []
    if settings.DB_BACKUP_BUCKET:
        return []
    return [
        Warning(
            "No hay bucket de backup, así que el volcado diario de la base no se guarda.",
            hint="Definí GS_BUCKET_NAME (o DB_BACKUP_BUCKET aparte) — ver CONFIG-PENDIENTE.md.",
            id="common.W003",
        )
    ]
