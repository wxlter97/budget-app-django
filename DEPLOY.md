# Deploy — Neon + Cloud Run + Vercel (sin Celery)

```
Vercel ──HTTPS──> Cloud Run (Django + Gunicorn) ──> Neon (Postgres)
  cliente Expo web   API REST /api/v1                 serverless
  gratis             escala a cero                    0.5 GB gratis
```

**Sin Celery**: las tareas programadas (recurrentes, cuotas, cierre de mes) no
corren solas. Se disparan a mano — ver [§6](#6-tareas-sin-celery).

> **Shell**: los comandos están en **bash** (git-bash en Windows sirve). La
> continuación de línea es `\`. Si usás PowerShell, cambiá cada `\` por un
> backtick `` ` `` — **no mezcles**: pegar backticks en bash rompe el comando
> (bash los interpreta como sustitución y cada `--flag` corre suelto).

---

## 0. Requisitos una sola vez

| Herramienta | Instalación |
|---|---|
| `gcloud` CLI | https://cloud.google.com/sdk/docs/install → `gcloud init` |
| Cuenta Neon | https://neon.tech |
| Cuenta Vercel | https://vercel.com |
| Node 20+ | ya lo tenés para `web/` |

```bash
gcloud auth login
gcloud config set project TU_PROJECT_ID          # p. ej. budget-wxlter
gcloud config set run/region us-east1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

---

## 1. Base de datos — Neon

1. **New Project** → nombre `budget`, región **AWS US East (N. Virginia)**.
2. En *Connection Details* copiá el connection string **Direct** (no el pooled).
3. Verificá que termina en `?sslmode=require`:
   ```
   postgres://budget_owner:npg_XXXX@ep-nombre-123456.us-east-1.aws.neon.tech/neondb?sslmode=require
   ```

Las migraciones corren solas al arrancar el contenedor; no hay que crear tablas.

---

## 2. Backend — Cloud Run

### 2.1 Secretos (una vez)

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))" \
  | gcloud secrets create django-secret-key --data-file=-

# pegá tu connection string de Neon entre las comillas:
printf %s 'postgres://budget_owner:npg_XXXX@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require' \
  | gcloud secrets create database-url --data-file=-
```

Para **actualizar** un secreto más adelante: `... | gcloud secrets versions add django-secret-key --data-file=-`

### 2.2 Deploy

```bash
cd budget
bash deploy-cloudrun.sh
```

El script corre el `gcloud run deploy` con todas las flags (secrets + env vars).
Anotá la URL que imprime: `https://budget-api-XXXXXXXX-ue.a.run.app`.

Comprobá:
- `https://budget-api-XXXX.a.run.app/healthz/` → `{"status":"ok"}`
- `https://budget-api-XXXX.a.run.app/api/docs/` → Swagger

> `CORS_ALLOWED_ORIGINS` se agrega en el [§4](#4-conectar-cors), cuando exista la
> URL de Vercel. Hasta entonces el front no puede llamar al API — es esperado.
>
> La revisión rota que quedó del intento fallido no molesta: el próximo deploy
> exitoso se lleva el tráfico.

### 2.3 Superusuario (solo para `/admin/`)

Para uso normal no hace falta: registrate desde la web. Para el admin de Django:

```bash
gcloud run jobs deploy budget-admin \
  --source . --region us-east1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0,DJANGO_SUPERUSER_USERNAME=admin,DJANGO_SUPERUSER_EMAIL=tu@correo.com,DJANGO_SUPERUSER_PASSWORD=una-clave-larga" \
  --command python --args "manage.py,createsuperuser,--noinput"

gcloud run jobs execute budget-admin --region us-east1 --wait
```

---

## 3. Frontend — Vercel

### Opción A — conectar el repo (recomendada)

1. Vercel → **Add New… → Project** → importá `wxlter97/moneyapp`.
2. Vercel lee `vercel.json`: **no toques** Build Command ni Output Directory.
3. **Environment Variables** → para *Production* y *Preview*:
   ```
   EXPO_PUBLIC_API_URL = https://budget-api-XXXXXXXX-ue.a.run.app/api/v1
   ```
4. **Deploy**. Anotá la URL: `https://moneyapp.vercel.app` (o la que asigne).

Cada push a `main` redeploya. Los PRs generan preview deployments.

### Opción B — CLI

```bash
cd web
npx vercel login
npx vercel link
npx vercel env add EXPO_PUBLIC_API_URL production   # pegás la URL .../api/v1
npm run deploy:web                                   # = npx vercel deploy --prod
```

---

## 4. Conectar CORS

Con la URL de Vercel ya conocida:

```bash
gcloud run services update budget-api --region us-east1 \
  --update-env-vars "CORS_ALLOWED_ORIGINS=https://moneyapp.vercel.app"
```

Dominio propio más adelante: sumá los orígenes con coma en `CORS_ALLOWED_ORIGINS`,
`DJANGO_ALLOWED_HOSTS` y `DJANGO_CSRF_TRUSTED_ORIGINS`.

---

## 5. Prueba de humo

1. Abrí la URL de Vercel.
2. Registro → login → workspace → cartera → transacción.
3. Recargá en `/dashboard` (verifica el rewrite SPA).
4. `https://budget-api-XXXX.a.run.app/admin/` con el superusuario.

La **primera** request del día tarda ~2-4 s (Cloud Run + Neon despiertan).

---

## 6. Tareas sin Celery

Los `@shared_task` se ejecutan sincrónicamente llamándolos como función:

```bash
gcloud run jobs deploy budget-cron \
  --source . --region us-east1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0" \
  --command python \
  --args "manage.py,shell,-c,from apps.transactions.tasks import generate_recurring_transactions, post_due_installments; from apps.reports.tasks import close_previous_month; generate_recurring_transactions(); post_due_installments(); close_previous_month()"

gcloud run jobs execute budget-cron --region us-east1 --wait
```

Para que corra solo cada día: **Cloud Scheduler → Cloud Run Job** (3 jobs gratis).

---

## 7. Limpieza y costos

- **Artifact Registry**: política de limpieza una vez (deja las 3 imágenes más nuevas):
  ```bash
  printf '[{"name":"keep-3","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}}]' > /tmp/cleanup.json
  gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
    --location us-east1 --policy-file /tmp/cleanup.json
  ```
- **Neon free**: 0.5 GB, ~190 h cómputo/mes, auto-suspende a los 5 min.
- **Cloud Run free**: 2 M req, 360 000 GiB-s, 180 000 vCPU-s al mes.
- **Vercel Hobby**: gratis uso no comercial, ~100 GB banda/mes.
- **Backups**: Neon free retiene ~24 h. Un `pg_dump` periódico si querés más.

---

## 8. Redeploys

| Qué | Comando |
|---|---|
| Backend | `bash deploy-cloudrun.sh` (las env vars persisten entre deploys) |
| Frontend | push a `main` (Opción A) o `npm run deploy:web` (Opción B) |
| Env var backend | `gcloud run services update budget-api --region us-east1 --update-env-vars "K=V"` |
| Ver logs del arranque | `gcloud run services logs read budget-api --region us-east1 --limit 50` |
