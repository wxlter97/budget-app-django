# Deploy — Neon + Cloud Run + Vercel (sin Celery)

Arquitectura de la puesta en producción para uso personal:

```
Vercel ──HTTPS──> Cloud Run (Django + Gunicorn) ──> Neon (Postgres)
  cliente Expo web   API REST /api/v1                 serverless
  gratis             escala a cero                    0.5 GB gratis
```

**Sin Celery**: las tareas programadas (recurrentes, cuotas, cierre de mes) no
corren solas. Se disparan a mano cuando haga falta — ver [§6](#6-tareas-sin-celery).

Todo cabe en las capas gratuitas para uso personal. El único costo posible es el
almacenamiento de imágenes viejas en Artifact Registry (ver [§7](#7-limpieza-y-costos)).

---

## 0. Requisitos una sola vez

| Herramienta | Instalación |
|---|---|
| `gcloud` CLI | https://cloud.google.com/sdk/docs/install → `gcloud init` |
| Cuenta Neon | https://neon.tech (login con GitHub) |
| Cuenta Vercel | https://vercel.com (login con GitHub) |
| Node 20+ | ya lo tenés para `web/` |

```powershell
gcloud auth login
gcloud config set project TU_PROJECT_ID
gcloud config set run/region us-east1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

> Región: `us-east1` va bien desde México y queda cerca de Neon `aws-us-east-1`.

---

## 1. Base de datos — Neon

1. **New Project** → nombre `budget`, región **AWS US East (N. Virginia)**.
2. En *Connection Details* copiá el connection string. Neon da dos:
   - **Direct** (`ep-xxx.us-east-1.aws.neon.tech`) — usá este.
   - Pooled (`ep-xxx-pooler...`) — solo si algún día ponés `--max-instances > 1`;
     en ese caso además `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True`.
3. Verificá que termina en `?sslmode=require`. Queda algo así:
   ```
   postgres://budget_owner:npg_XXXX@ep-cool-name-123456.us-east-1.aws.neon.tech/neondb?sslmode=require
   ```

No hace falta crear tablas: las migraciones corren solas al arrancar el contenedor.

---

## 2. Backend — Cloud Run

### 2.1 Secretos

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))" | gcloud secrets create django-secret-key --data-file=-

# pegá tu connection string de Neon entre las comillas
"postgres://budget_owner:npg_XXXX@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require" | gcloud secrets create database-url --data-file=-
```

### 2.2 Primer deploy

Desde `budget/` (usa el `Dockerfile`):

```powershell
gcloud run deploy budget-api `
  --source . `
  --region us-east1 `
  --allow-unauthenticated `
  --max-instances 1 `
  --cpu 1 --memory 1Gi --cpu-boost `
  --concurrency 8 `
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" `
  --set-env-vars "DJANGO_DEBUG=False,DJANGO_ALLOWED_HOSTS=.run.app,DJANGO_CSRF_TRUSTED_ORIGINS=https://*.run.app,DJANGO_DB_CONN_MAX_AGE=0,DJANGO_TIME_ZONE=America/Mexico_City,DJANGO_LANGUAGE_CODE=es"
```

Anotá la URL que imprime: `https://budget-api-XXXXXXXX-ue.a.run.app`.

> `CORS_ALLOWED_ORIGINS` se agrega en el [§4](#4-conectar-cors), cuando ya exista
> la URL de Vercel. Hasta entonces el front no puede llamar al API — es esperado.

Comprobá:
- `https://budget-api-XXXX.a.run.app/healthz/` → `{"status":"ok"}`
- `https://budget-api-XXXX.a.run.app/api/docs/` → Swagger

### 2.3 Usuario

Para **uso normal** no necesitás nada: registrate desde la web (`/auth/register/`).

Para el **admin de Django** (`/admin/`) creá un superusuario con un Job:

```powershell
gcloud run jobs deploy budget-admin `
  --source . --region us-east1 `
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" `
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0,DJANGO_SUPERUSER_USERNAME=admin,DJANGO_SUPERUSER_EMAIL=tu@correo.com,DJANGO_SUPERUSER_PASSWORD=una-clave-larga" `
  --command python --args "manage.py,createsuperuser,--noinput"

gcloud run jobs execute budget-admin --region us-east1 --wait
```

---

## 3. Frontend — Vercel

### Opción A — conectar el repo (recomendada)

Requiere que `web/` tenga remoto en GitHub.

1. Vercel → **Add New… → Project** → importá el repo `web/`.
2. Vercel lee `vercel.json`, así que **no toques** Build Command ni Output
   Directory (ya vienen: `expo export -p web` → `dist`, con rewrite SPA).
3. **Environment Variables** → agregá para *Production* y *Preview*:
   ```
   EXPO_PUBLIC_API_URL = https://budget-api-XXXXXXXX-ue.a.run.app/api/v1
   ```
4. **Deploy**. Anotá la URL: `https://budget-web.vercel.app` (o el nombre que
   elijas).

A partir de acá, cada push a `main` redeploya solo. Los PRs generan *preview
deployments*.

### Opción B — deploy manual con la CLI

Sin necesidad de repo en GitHub:

```powershell
cd web
npx vercel login            # una sola vez
npx vercel link             # crea/asocia el proyecto
npx vercel env add EXPO_PUBLIC_API_URL production
# pegás: https://budget-api-XXXXXXXX-ue.a.run.app/api/v1
npm run deploy:web          # = npx vercel deploy --prod
```

El build corre en Vercel (usa el `buildCommand` de `vercel.json`).

---

## 4. Conectar CORS

Ya con la URL de Vercel, actualizá el backend:

```powershell
gcloud run services update budget-api --region us-east1 `
  --update-env-vars "CORS_ALLOWED_ORIGINS=https://budget-web.vercel.app"
```

Si más adelante ponés dominio propio, sumá los orígenes separados por coma en
`CORS_ALLOWED_ORIGINS`, `DJANGO_ALLOWED_HOSTS` y `DJANGO_CSRF_TRUSTED_ORIGINS`.

> Los *preview deployments* de Vercel usan URLs `https://budget-web-<hash>.vercel.app`.
> Si querés que funcionen contra este backend, agregá también
> `https://budget-web-*.vercel.app` — pero `CORS_ALLOWED_ORIGINS` no acepta
> comodines; para previews conviene un backend aparte o probar en local.

---

## 5. Prueba de humo

1. Abrí `https://budget-web.vercel.app`.
2. Registro → login → crear workspace → crear cartera → crear transacción.
3. Recargá en `/dashboard` (verifica el rewrite SPA de Vercel).
4. `https://budget-api-XXXX.a.run.app/admin/` con el superusuario.

La **primera** request del día tarda ~2-4 s (Cloud Run despierta el contenedor +
Neon despierta la DB). Después, normal.

---

## 6. Tareas sin Celery

Los `@shared_task` se pueden ejecutar sincrónicamente llamándolos como función.
Un Job que las corre las tres:

```powershell
gcloud run jobs deploy budget-cron `
  --source . --region us-east1 `
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" `
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0" `
  --command python `
  --args "manage.py,shell,-c,from apps.transactions.tasks import generate_recurring_transactions, post_due_installments; from apps.reports.tasks import close_previous_month; generate_recurring_transactions(); post_due_installments(); close_previous_month()"

gcloud run jobs execute budget-cron --region us-east1 --wait
```

Si querés que corra solo cada día: **Cloud Scheduler → Cloud Run Job** (también
dentro de free tier, 3 jobs de scheduler gratis).

---

## 7. Limpieza y costos

- **Artifact Registry**: cada `--source` deja una imagen en el repo
  `cloud-run-source-deploy`. Poné una política de limpieza una vez:
  ```powershell
  gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy `
    --location us-east1 --policy-file - <<< '[{"name":"keep-3","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}}]'
  ```
- **Neon free**: 0.5 GB de datos y ~190 h de cómputo/mes. Un presupuesto personal
  usa unos pocos MB. Auto-suspende a los 5 min (no se puede desactivar en free).
- **Cloud Run free**: 2 M requests, 360 000 GiB-s, 180 000 vCPU-s al mes. Uso
  personal ni lo roza.
- **Vercel Hobby**: gratis para uso no comercial. ~100 GB de ancho de banda y
  6000 min de build al mes; de sobra.
- **Backups**: Neon free retiene ~24 h de historial. Para más tranquilidad, un
  `pg_dump` periódico a donde quieras.

---

## 8. Redeploys

| Qué | Comando |
|---|---|
| Backend | `gcloud run deploy budget-api --source . --region us-east1` (las env vars persisten) |
| Frontend | push a `main` (Opción A), o `npm run deploy:web` (Opción B) |
| Cambiar una env var del backend | `gcloud run services update budget-api --region us-east1 --update-env-vars "CLAVE=valor"` |
| Cambiar `EXPO_PUBLIC_API_URL` | panel de Vercel → Settings → Environment Variables, y redeploy |
