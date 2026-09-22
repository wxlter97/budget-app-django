# Fijada a la suite de Debian y no a `python:3.12-slim` a secas: esa etiqueta
# se movió sola de bookworm a trixie, y con ella se rompió el build de abajo.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pg_dump para el backup diario (`manage.py backup_database`, ver RUNBOOK.md).
# pg_dump se niega a volcar un servidor de una versión mayor que la suya, así
# que el cliente tiene que ser >= el de Neon. Neon pasó a 18 (18.6 al 19-sep-2026)
# y Debian 13 (trixie, la base de arriba) sólo trae el 17 en sus repos: el
# backup diario falló con "aborting because of server version mismatch". Por eso
# ahora el cliente sale de PGDG, el repositorio oficial de PostgreSQL. Un cliente
# más nuevo que el servidor sí funciona, así que el 18 sirve también si la base
# baja a 15, 16 o 17.
#
# Al subir de versión en Neon, subir PG_CLIENT_MAJOR acá. La suite de PGDG se
# saca de ${VERSION_CODENAME} de /etc/os-release, no se escribe a mano, que es
# exactamente lo que rompió este build la primera vez.
#
# El `else` no es adorno: si PGDG no responde (llave, red, paquete), el build no
# se cae ni frena el deploy de la app; queda el 17 de Debian y el aviso en el log
# del build. De los dos fallos posibles éste es el barato: el que falla después
# es el backup diario, con el mensaje textual de pg_dump.
ARG PG_CLIENT_MAJOR=18
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    . /etc/os-release; \
    if install -d /usr/share/postgresql-common/pgdg \
      && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
           https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
           > /etc/apt/sources.list.d/pgdg.list \
      && apt-get update \
      && apt-get install -y --no-install-recommends "postgresql-client-${PG_CLIENT_MAJOR}"; then \
      :; \
    else \
      echo "AVISO: no se pudo instalar postgresql-client-${PG_CLIENT_MAJOR} desde PGDG; se usa el de Debian (el backup puede fallar por versión)." >&2; \
      rm -f /etc/apt/sources.list.d/pgdg.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends postgresql-client-17 \
        || apt-get install -y --no-install-recommends postgresql-client; \
    fi; \
    pg_dump --version; \
    # `curl` se queda instalado A PROPÓSITO por ahora (normalmente se purga acá):
    # `wompi_probe --diagnostico-red` lo necesita para distinguir un bloqueo por IP de
    # uno por la huella TLS del cliente Python. Revertir (volver a purgarlo) en cuanto
    # se resuelva el bloqueo de Wompi -- ver budget-app-django, PR de este commit.
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
