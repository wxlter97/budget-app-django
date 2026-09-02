#!/bin/sh
# Arranque del contenedor en Cloud Run.
#
# Cloud Run no tiene "release phase", así que las migraciones se corren aquí al
# iniciar. A escala personal (--max-instances 1) no hay carrera entre instancias;
# si algún día molesta, poné RUN_MIGRATIONS=0 y corré las migraciones con un
# Cloud Run Job aparte.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "==> migrate"
  python manage.py migrate --noinput
fi

# Cloud Run inyecta $PORT (8080). En local cae a 8000.
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-60}" \
  --access-logfile - \
  --error-logfile -
