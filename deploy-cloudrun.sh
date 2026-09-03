#!/usr/bin/env bash
# Deploy del backend a Cloud Run. Corré esto desde budget/ con:
#   bash deploy-cloudrun.sh
#
# Requiere antes (una vez):
#   - gcloud init + gcloud config set project TU_PROJECT
#   - los secrets `django-secret-key` y `database-url` creados (ver DEPLOY.md §2.1)
#
# Variables ajustables por entorno:
#   REGION (us-east1)   SERVICE (budget-api)   TZ (America/Mexico_City)
set -euo pipefail

REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-budget-api}"
TZ="${TZ:-America/Mexico_City}"

PROJECT="$(gcloud config get-value project 2>/dev/null)"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# La service account de runtime de Cloud Run tiene que poder leer los secrets.
# add-iam-policy-binding es idempotente: si ya está, no pasa nada.
echo "==> Concediendo acceso a los secrets a ${RUNTIME_SA}"
for secret in django-secret-key database-url; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
done

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --max-instances 1 \
  --cpu 1 \
  --memory 1Gi \
  --cpu-boost \
  --concurrency 8 \
  --timeout 300 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,DJANGO_ALLOWED_HOSTS=.run.app,DJANGO_CSRF_TRUSTED_ORIGINS=https://*.run.app,DJANGO_DB_CONN_MAX_AGE=0,DJANGO_TIME_ZONE=${TZ},DJANGO_LANGUAGE_CODE=es"

echo
echo "==> Listo. Ahora conectá CORS con la URL de Vercel (DEPLOY.md §4):"
echo "    gcloud run services update ${SERVICE} --region ${REGION} \\"
echo "      --update-env-vars \"CORS_ALLOWED_ORIGINS=https://TU-APP.vercel.app\""
