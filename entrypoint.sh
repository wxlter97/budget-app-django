#!/bin/sh
# Arranque del contenedor en Cloud Run.
#
# Cloud Run no tiene "release phase", así que las migraciones se corren aquí al
# iniciar. Con --max-instances > 1, dos instancias que arrancan a la vez pueden
# intentar migrar en paralelo: Postgres hace el DDL en transacción, así que no
# corrompe nada, pero la instancia que pierde falla el arranque y Cloud Run la
# reintenta (unos 502 justo después de un deploy que trae migraciones). Para
# sacarse eso de encima: RUN_MIGRATIONS=0 en el servicio y migrar con el Job
# `budget-migrate` antes de deployar (ver DEPLOY.md §2.2).
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "==> migrate"
  python manage.py migrate --noinput
fi

# Avisos de configuración de producción (cache compartido, endpoint pooled de
# Neon, chequeos de seguridad de Django). Los tags dejan fuera el ruido de
# drf-spectacular, que sólo importa en desarrollo. Nunca bloquea el arranque:
# sólo deja el aviso en los logs de Cloud Run.
python manage.py check --deploy --tag caches --tag database --tag security || true

# Cloud Run inyecta $PORT (8080). En local cae a 8000.
#
# `--header-map dangerous`: Wompi firma sus webhooks con la cabecera `wompi_hash` (con
# guion bajo) y Gunicorn, por defecto, DESCARTA en silencio toda cabecera con guion bajo:
# sin esto `verify_webhook` nunca la vería y rechazaría todos los avisos. No abre un hueco
# nuevo: el webhook se autentica con la firma HMAC, no con la cabecera en sí.
exec gunicorn config.wsgi:application \
  --header-map dangerous \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-60}" \
  --access-logfile - \
  --error-logfile -
