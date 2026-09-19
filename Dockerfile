# Fijada a la suite de Debian y no a `python:3.12-slim` a secas: esa etiqueta
# se movió sola de bookworm a trixie, y con ella se rompió el build de abajo.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pg_dump para el backup diario (`manage.py backup_database`, ver RUNBOOK.md).
# pg_dump se niega a volcar un servidor de una versión mayor que la suya, así
# que el cliente tiene que ser >= el de Neon (los proyectos nuevos corren 17).
# Debian 13 (trixie, la base de arriba) ya trae el 17 en sus propios repos, así
# que no hace falta el repositorio de PGDG: un apt-get y nada de descargar
# llaves en tiempo de build. Un cliente más nuevo que el servidor sí funciona,
# así que el 17 sirve también si la base es 15 o 16.
#
# Si algún día Neon pasa a 18 y Debian todavía no lo tiene, ahí sí toca PGDG
# -- y entonces la suite se saca de ${VERSION_CODENAME} de /etc/os-release, no
# se escribe a mano, que es exactamente lo que rompió este build la primera vez.
#
# El `||` no es adorno: `postgresql-client` (el metapaquete, que apunta a la
# versión por defecto de la release) existe en cualquier Debian, y el build no
# se puede permitir caerse por el nombre de un paquete. De los dos fallos
# posibles, éste es el barato: si alguna vez quedara un cliente viejo, el que
# falla es el backup diario, con el mensaje textual de pg_dump y sin tocar el
# deploy de la app.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17 \
      || apt-get install -y --no-install-recommends postgresql-client; \
    pg_dump --version; \
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
