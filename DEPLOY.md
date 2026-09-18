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

El `deploy-cloudrun.sh` le concede a la service account de runtime de Cloud Run
(`PROJECT_NUMBER-compute@developer.gserviceaccount.com`) el rol
`roles/secretmanager.secretAccessor` sobre ambos secrets (idempotente). Si
preferís hacerlo a mano:

```bash
PN=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
for s in django-secret-key database-url; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

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

El servicio queda con `--max-instances 5`. Las migraciones corren al arrancar
cada contenedor (`entrypoint.sh`), así que con varias instancias un deploy que
trae migraciones puede dar unos 502 mientras la instancia que perdió la carrera
reintenta. Para sacárselo de encima (y de paso acelerar el arranque en frío),
migrar aparte y dejar al servicio sin migrar:

```bash
gcloud run jobs deploy budget-migrate \
  --source . --region us-east1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0" \
  --command python --args "manage.py,migrate,--noinput"

# En cada release que traiga migraciones, antes del deploy:
gcloud run jobs execute budget-migrate --region us-east1 --wait

# Y una vez, para que las instancias no vuelvan a migrar al arrancar:
gcloud run services update budget-api --region us-east1 \
  --update-env-vars "RUN_MIGRATIONS=0"
```

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

### 2.4 Planes de billing (una sola vez, la primera vez que se activa)

`seed_billing_plans` crea los planes Free/Pro -- correrlo es seguro en
cualquier momento (es idempotente). Pero si esto es un entorno que **ya
tenía usuarios reales** antes de que existiera billing, corré también
`grandfather_existing_users` justo después (ver la docstring del comando
para el porqué: sin esto, alguien que ya usaba una función Pro-only la
pierde de un día para el otro):

```bash
gcloud run jobs deploy budget-admin \
  --source . --region us-east1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0" \
  --command python --args "manage.py,seed_billing_plans"
gcloud run jobs execute budget-admin --region us-east1 --wait

# Sólo si ya había usuarios reales antes de esto -- primero en dry-run:
gcloud run jobs update budget-admin --region us-east1 \
  --command python --args "manage.py,grandfather_existing_users,--dry-run"
gcloud run jobs execute budget-admin --region us-east1 --wait
gcloud run jobs update budget-admin --region us-east1 \
  --command python --args "manage.py,grandfather_existing_users"
gcloud run jobs execute budget-admin --region us-east1 --wait
```

---

## 3. Frontend — Vercel o Cloudflare Pages

> **Ojo con el plan Hobby de Vercel: no permite uso comercial.** Desde el momento
> en que se cobra una suscripción hay que pasar a Pro ($20/mes) o mover el front a
> otro lado. El build es estático (`expo export -p web`, sin SSR ni funciones), así
> que **Cloudflare Pages** hace exactamente lo mismo gratis, con banda ilimitada y
> uso comercial permitido — ver Opción C y `COSTOS-Y-ESCALA.md`.

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

### Opción C — Cloudflare Pages (gratis y sí permite uso comercial)

Cloudflare → **Workers & Pages → Create → Pages → Connect to Git** → `wxlter97/moneyapp`:

| Campo | Valor |
|---|---|
| Build command | `npx expo export -p web && node scripts/pwa-postbuild.js` |
| Build output directory | `dist` |
| Variable de entorno | `EXPO_PUBLIC_API_URL = https://budget-api-XXXX.a.run.app/api/v1` |

Pages instala las dependencias solo a partir del lockfile, así que el build
command no lleva `npm ci`. La imagen de build trae Node 22 por defecto, que es
lo que usa el repo; si algún día no coincide, se fija con la variable
`NODE_VERSION`.

El rewrite de SPA que en Vercel vive en `vercel.json` (todas las rutas a
`index.html`) **ya está en el repo**: `moneyapp/public/_redirects`. Va en
`public/` porque `expo export` copia ese directorio tal cual a `dist/` — igual
que `sw.js` y `manifest.webmanifest` — y Pages lo lee desde la raíz del output
sin servirlo como contenido. Los dos archivos conviven: Vercel ignora
`_redirects` y Pages ignora `vercel.json`, así que mover el hosting no toca el
build.

También hay que cambiar el script `deploy:web` de `package.json`, que hoy es
`npx vercel deploy --prod`, por `npx wrangler pages deploy dist`.

Después, en el §4, `CORS_ALLOWED_ORIGINS` apunta al dominio de Pages en vez del
de Vercel. El resto del deploy no cambia.

---

## 4. Conectar CORS

Con la URL de Vercel ya conocida:

```bash
gcloud run services update budget-api --region us-east1 \
  --update-env-vars "CORS_ALLOWED_ORIGINS=https://moneyapp.vercel.app"
```

Dominio propio más adelante: sumá los orígenes con coma en `CORS_ALLOWED_ORIGINS`,
`DJANGO_ALLOWED_HOSTS` y `DJANGO_CSRF_TRUSTED_ORIGINS`.

### 4.1 Dashboard externo (Villa Wxlter)

El endpoint `GET /api/v1/dashboard/balance/` (patrimonio neto, sin JWT) lo
consume el mapa de Villa Wxlter para el tooltip del edificio Banco. Necesita
su propio token — no reutiliza login de usuario:

```bash
gcloud run services update budget-api --region us-east1 \
  --update-secrets "DASHBOARD_API_TOKEN=dashboard-api-token:latest" \
  --update-env-vars "CORS_ALLOWED_ORIGINS=https://moneyapp.vercel.app,https://villa-wxlter.vercel.app"
```

- Generar el token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`,
  guardarlo como secret `dashboard-api-token` (igual que `django-secret-key`,
  ver §2.1) y dárselo al dashboard como variable de entorno en Vercel.
- `DASHBOARD_WORKSPACE_ID` solo hace falta si alguna vez hay más de un
  Workspace en la base — con uno solo (el caso de uso real) el endpoint lo
  detecta automáticamente y falla explícito si es ambiguo, en vez de mostrar
  el saldo equivocado.

---

## 5. Prueba de humo

1. Abrí la URL de Vercel.
2. Registro → login → workspace → cartera → transacción.
3. Recargá en `/dashboard` (verifica el rewrite SPA).
4. `https://budget-api-XXXX.a.run.app/admin/` con el superusuario.

La **primera** request del día tarda ~2-4 s (Cloud Run + Neon despiertan).

---

## 6. Tareas sin Celery

`manage.py run_daily_tasks` corre las tareas periódicas en el orden correcto
(recurrentes → cierres → recordatorios → backup de la base, ver
`apps/common/management/commands/run_daily_tasks.py`) sincrónicamente, sin
broker. Todas son idempotentes: no pasa nada si el job corre dos veces el
mismo día, o a una hora que no es la ideal.

El backup va último a propósito: si `pg_dump` falla o el bucket rechaza la
subida, el job queda marcado como fallido en Cloud Run — que es como uno se
entera — pero para entonces los recordatorios ya salieron. Necesita
`GS_BUCKET_NAME` (o `DB_BACKUP_BUCKET`) en el Job, si no avisa y no hace nada.
Restaurar desde un volcado: `RUNBOOK.md` §9.

### 6.1 Cloud Run Job (una sola vez)

```bash
gcloud run jobs deploy budget-cron \
  --source . --region us-east1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0,GS_BUCKET_NAME=budget-recibos-prod" \
  --command python \
  --args "manage.py,run_daily_tasks"

# La service account del job necesita escribir en el bucket (para el backup):
gcloud storage buckets add-iam-policy-binding gs://budget-recibos-prod \
  --member "serviceAccount:$(gcloud projects describe $(gcloud config get-value project) \
      --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role roles/storage.objectAdmin

gcloud run jobs execute budget-cron --region us-east1 --wait   # probarlo a mano una vez
```

### 6.2 Cloud Scheduler → Cloud Run Job (para que corra solo cada día)

Un Cloud Scheduler que dispara el Job de arriba todos los días a las 7am
hora de El Salvador. Necesita su propia service account con permiso para
ejecutar ESE job (no el rol amplio de administrar Cloud Run):

```bash
# Habilitar la API (una sola vez por proyecto)
gcloud services enable cloudscheduler.googleapis.com

# Service account dedicada, sólo con permiso de invocar este job puntual
gcloud iam service-accounts create budget-cron-invoker \
  --display-name "Invoca budget-cron desde Cloud Scheduler"

gcloud run jobs add-iam-policy-binding budget-cron \
  --region us-east1 \
  --member "serviceAccount:budget-cron-invoker@TU_PROJECT_ID.iam.gserviceaccount.com" \
  --role "roles/run.invoker"

# El propio Scheduler, apuntando al endpoint `:run` de la Admin API de Cloud Run
gcloud scheduler jobs create http budget-cron-daily \
  --location us-east1 \
  --schedule "0 7 * * *" \
  --time-zone "America/El_Salvador" \
  --uri "https://us-east1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/TU_PROJECT_ID/jobs/budget-cron:run" \
  --http-method POST \
  --oauth-service-account-email "budget-cron-invoker@TU_PROJECT_ID.iam.gserviceaccount.com"
```

Verificar que quedó armado (y forzar una corrida sin esperar a mañana):

```bash
gcloud scheduler jobs describe budget-cron-daily --location us-east1
gcloud scheduler jobs run budget-cron-daily --location us-east1
gcloud run jobs executions list --job budget-cron --region us-east1   # ver que corrió
```

Todavía entra en el tier gratis (3 jobs de Scheduler, 2M invocaciones de
Cloud Run al mes).

### 6.3 Mantener caliente el arranque (opcional, recomendado con usuarios)

Cloud Run en 0 instancias y Neon auto-suspendido a los 5 minutos hacen que la
primera visita después de un rato pague el arranque de Django **más** el
despertar de la base. `--min-instances 1` lo arregla a medias y cuesta del
orden de $50-70/mes; un ping cada 5 minutos en horario activo cuesta centavos
y despierta a los dos:

```bash
gcloud scheduler jobs create http budget-keepalive \
  --location us-east1 \
  --schedule "*/5 11-23 * * *" \
  --time-zone "Etc/UTC" \
  --uri "https://budget-api-XXXX.a.run.app/healthz/" \
  --http-method GET
```

`11-23` UTC es 5am-5pm en El Salvador. Es el tercer job de Scheduler: sigue
entrando en los 3 gratis.

Ojo: `/healthz/` **no toca la base** (es un `JsonResponse` fijo, ver
`config/urls.py`), así que esto mantiene caliente Cloud Run pero no Neon --
que es la mitad más grande del arranque en frío, igual. Para despertar también
a Neon, apuntá el ping a un endpoint que consulte, por ejemplo
`/api/v1/dashboard/balance/` con su `DASHBOARD_API_TOKEN` en un header.

---

## 7. Limpieza y costos

- **Artifact Registry**: política de limpieza una vez (deja las 3 imágenes más nuevas):
  ```bash
  printf '[{"name":"keep-3","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}}]' > /tmp/cleanup.json
  gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
    --location us-east1 --policy-file /tmp/cleanup.json
  ```
- **Neon free**: 0.5 GB, 100 h cómputo/mes, auto-suspende a los 5 min.
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

---

## 9. Deploy automático al hacer merge a `main`

### Backend — GitHub Actions + Workload Identity Federation

`.github/workflows/deploy.yml` corre `gcloud run deploy --source .` en cada push a
`main`. No usa claves de service account: autentica por OIDC.

**Setup (una vez):**

```bash
cd budget
bash scripts/setup-gh-deploy.sh
```

Imprime `WIF_PROVIDER` y `DEPLOY_SA`. Guardalos en GitHub:
**repo → Settings → Secrets and variables → Actions → pestaña _Variables_** →
`New repository variable` para cada uno.

Después, cada merge a `main` dispara el workflow *Deploy* (pestaña Actions).
Hace build, deploy, corre migraciones al arrancar (`entrypoint.sh`) y un
smoke test de `/healthz/`. Para lanzarlo a mano: Actions → Deploy → *Run workflow*.

> El workflow **no** pasa `--set-env-vars` ni `--set-secrets`: `gcloud run deploy`
> conserva la config existente (incluido `CORS_ALLOWED_ORIGINS`). Para *cambiar*
> una env var seguí usando `deploy-cloudrun.sh` o `gcloud run services update`.

### Frontend — Vercel (integración de Git)

Si el proyecto de Vercel está **conectado al repo** `wxlter97/moneyapp`
(Vercel → Project → Settings → Git), cada push a `main` ya redeploya a producción
y cada PR genera un *preview* — no hay que hacer nada.

Comprobalo en el dashboard de Vercel. Si no está conectado:
Vercel → *Add New… → Project* → importá el repo → en *Environment Variables*
poné `EXPO_PUBLIC_API_URL = https://budget-api-dssz7o3ila-ue.a.run.app/api/v1`
→ Deploy. `vercel.json` ya trae build command y output dir.

El repo web también tiene `.github/workflows/ci.yml` (typecheck + tests + build)
que corre en cada PR y push a `main`.
