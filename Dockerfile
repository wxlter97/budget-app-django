FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

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
