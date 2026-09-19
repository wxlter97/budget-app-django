FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pg_dump para el backup diario (`manage.py backup_database`, ver RUNBOOK.md).
# No alcanza con el `postgresql-client` de Debian 12: trae la versión 15 y
# pg_dump se niega a volcar un servidor más nuevo que él ("server version
# mismatch"), mientras que los proyectos nuevos de Neon corren 17. De ahí el
# repositorio de PGDG. Un cliente más nuevo que el servidor sí funciona, así
# que el 17 sirve también si la base es 15 o 16.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Estáticos al construir la imagen (admin, DRF, swagger). SECRET_KEY/DB no
# hacen falta para collectstatic; se usa el valor por defecto.
RUN python manage.py collectstatic --noinput

RUN useradd --create-home app && chown -R app /app
USER app

# Cloud Run enruta al puerto de $PORT (8080 por defecto); entrypoint.sh lo honra.
EXPOSE 8080
CMD ["sh", "/app/entrypoint.sh"]
