#!/usr/bin/env bash
# Configura el deploy automático de Cloud Run desde GitHub Actions usando
# Workload Identity Federation (sin claves de service account de larga vida).
#
# Corré esto UNA sola vez, con `gcloud` autenticado como dueño del proyecto:
#   bash scripts/setup-gh-deploy.sh
#
# Al final imprime dos valores. Guardalos como **Variables** del repo en GitHub
# (Settings → Secrets and variables → Actions → Variables):
#   WIF_PROVIDER   y   DEPLOY_SA
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
REPO="${REPO:-wxlter97/budget-app-django}"      # owner/repo de GitHub
REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-budget-api}"

POOL="github-pool"
PROVIDER="github-provider"
SA_NAME="gh-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
OWNER="${REPO%%/*}"

echo "==> Proyecto ${PROJECT} (${PROJECT_NUMBER}), repo ${REPO}"

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT"

# --- Workload Identity Pool + provider (OIDC de GitHub) ---------------------
gcloud iam workload-identity-pools create "$POOL" \
  --project "$PROJECT" --location=global --display-name="GitHub Actions" \
  2>/dev/null || echo "   (pool ya existe)"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --project "$PROJECT" --location=global --workload-identity-pool="$POOL" \
  --display-name="GitHub" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner=='${OWNER}'" \
  2>/dev/null || echo "   (provider ya existe)"

# --- Service account que hace el deploy ------------------------------------
gcloud iam service-accounts create "$SA_NAME" \
  --project "$PROJECT" --display-name="GitHub Actions deployer" \
  2>/dev/null || echo "   (service account ya existe)"

for role in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/storage.admin \
  roles/iam.serviceAccountUser \
  roles/logging.viewer
do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" \
    --condition=None --quiet >/dev/null
done

# actuar como la service account de runtime de Cloud Run
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --project "$PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" --quiet >/dev/null

# --- Dejar que SOLO este repo de GitHub use la service account -------------
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project "$PROJECT" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" \
  --quiet >/dev/null

echo
echo "================  Variables para GitHub (repo → Settings → Actions → Variables)  ================"
echo "WIF_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "DEPLOY_SA    = ${SA_EMAIL}"
echo "=============================================================================================="
