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
2. En *Connection Details* activá **Connection pooling** y copiá el connection string
   **pooled** (el host lleva `-pooler`). Es el que usa producción, y es el que pide
   el check `common.W002`; además hace falta `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True`
   (ver `CONFIG-PENDIENTE.md`). Las migraciones y el `pg_dump` del backup funcionan
   igual por ahí.
3. Verificá que termina en `?sslmode=require`:
   ```
   postgres://budget_owner:npg_XXXX@ep-nombre-123456-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
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

El script deja el servicio con `--max-instances 5`; **producción corre hoy con 1**
(verificado el 19-sep-2026, se bajó a mano). Las migraciones corren al arrancar
cada contenedor (`entrypoint.sh`), así que con varias instancias un deploy que
trae migraciones puede dar unos 502 mientras la instancia que perdió la carrera
reintenta. Para sacárselo de encima (y de paso acelerar el arranque en frío),
migrar aparte y dejar al servicio sin migrar:

```bash
# Una vez. --max-retries 0: una migración que falló no se reintenta sola.
gcloud run jobs deploy budget-migrate \
  --source . --region us-east1 \
  --max-retries 0 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0" \
  --command python --args "manage.py,migrate,--noinput"
```

El workflow de deploy (`.github/workflows/deploy.yml`) ya lo usa: despliega la
revisión **sin tráfico**, corre `budget-migrate` con esa misma imagen y sólo si
sale bien le pasa el tráfico. Si `budget-migrate` no existe, se omite y migra el
arranque del contenedor, como antes. Cuando el job exista y un deploy ya haya
pasado por ahí, se apaga la migración al arrancar (una sola vez):

```bash
gcloud run services update budget-api --region us-east1 \
  --update-env-vars "RUN_MIGRATIONS=0"
```

Consecuencia que hay que tener presente: la migración corre **antes** de que el
código nuevo reciba tráfico, así que la revisión vieja tiene que seguir andando
contra el esquema nuevo durante esos minutos. Nunca borrar ni renombrar una
columna en el mismo release que deja de usarla: primero se deja de usar, y en
el release siguiente se borra. Es exactamente lo que rompió a `budget-cron`
con la migración 0026.

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

### 2.3b Catálogo de lealtad (bancos, tarjetas, tasas)

**El admin (Lealtad) es la fuente de verdad** del catálogo: bancos, tarjetas, programas,
tasas, comercios y alias se agregan y se corrigen ahí, sin deploy. `seed_loyalty_catalog`
carga `apps/loyalty/catalog.py` y por defecto **sólo crea lo que falta**: nunca modifica,
revive ni borra lo que ya existe, así que correrlo de nuevo no deshace nada hecho en el
admin. Tiene `--dry-run`.

```bash
gcloud run jobs update budget-admin --region us-east1 \
  --command python --args "manage.py,seed_loyalty_catalog,--dry-run"
gcloud run jobs execute budget-admin --region us-east1 --wait
gcloud run jobs update budget-admin --region us-east1 \
  --command python --args "manage.py,seed_loyalty_catalog"
gcloud run jobs execute budget-admin --region us-east1 --wait
```

`--actualizar` es la excepción deliberada: pisa lo existente con lo de `catalog.py`, revive
lo borrado y renombra los productos que se habían cargado a mano con otro nombre
(`RENAMES`). Sirve **una vez**, para la primera carga sobre datos que tenían errores (p. ej.
la tarjeta UNO con 6 % de descuento en todo, o EconoMía con 5 % de tasa base), o para
restaurar el catálogo entero. Pisa las correcciones hechas en el admin; córrelo primero
con `--dry-run` y sólo sabiéndolo.

Después, tres pasos, siempre primero con `--dry-run`:

1. `map_categories_to_rubros`: asigna el rubro de lealtad a las categorías que no
   tienen uno (sin rubro, una categoría no gana tasas especiales). Nunca pisa uno
   elegido a mano. Las categorías por defecto de un workspace nuevo ya nacen mapeadas.
2. `recompute_loyalty_earnings`: calcula los puntos y el cashback de los gastos que
   **ya existían**. La señal de lealtad sólo corre al guardar un gasto, así que sin
   esto lo gastado antes de cargar el catálogo nunca aparece en "Recompensas".
3. Revisar el resultado en la app: pantalla de la tarjeta y Herramientas → Recompensas.

```bash
for CMD in "map_categories_to_rubros,--dry-run" "map_categories_to_rubros" \
           "recompute_loyalty_earnings,--dry-run" "recompute_loyalty_earnings"; do
  gcloud run jobs update budget-admin --region us-east1 --command python --args "manage.py,$CMD"
  gcloud run jobs execute budget-admin --region us-east1 --wait
done
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

### 2.5 Bucket de GCS — recibos y backups (una sola vez)

Un solo bucket privado cubre las dos cosas que hoy no tienen dónde vivir:

- **Los recibos** (`Transaction.receipt`). Sin bucket se guardan en el disco del
  contenedor, que en Cloud Run es efímero: **se borran en cada deploy**.
- **El volcado diario de la base** (`manage.py backup_database`, §6), que va al
  prefijo `backups/db/` del mismo bucket. Sin él el único respaldo son las ~24 h
  de historial que retiene Neon en plan free.

Es un bucket, no dos, porque el código ya usa `GS_BUCKET_NAME` para las dos
cosas. Si algún día querés separarlos —el argumento no es el costo sino el
radio de explosión: que la service account del servicio web no pueda leerse un
volcado entero de la base— existe `DB_BACKUP_BUCKET` y no hace falta tocar nada
más.

**La región se elige una sola vez y no se puede cambiar.** Tiene que ser la
misma de Cloud Run (`us-east1`): entre un bucket y un servicio de la misma
región el tráfico no se cobra, y desde otra región se paga egreso en **cada
lectura de recibo**. Es, de lejos, la decisión más cara de este bloque, y la
única irreversible: mover un bucket es crear otro y copiar todo.

```bash
BUCKET=budget-recibos-prod    # tiene que ser único en todo GCS, no sólo en tu proyecto

gcloud storage buckets create gs://$BUCKET \
  --location us-east1 \
  --default-storage-class STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention

# Autoclass: Google mueve solo cada objeto a la clase más barata según hace
# cuánto que nadie lo lee, y lo sube de vuelta al leerlo (ver más abajo).
gcloud storage buckets update gs://$BUCKET \
  --enable-autoclass --autoclass-terminal-storage-class ARCHIVE

# Aborta a las 24 h las subidas que quedaron a medias (una foto a medio subir
# se factura igual). Ver infra/README.md.
gcloud storage buckets update gs://$BUCKET --lifecycle-file=infra/gcs-lifecycle.json
```

> Si tu `gcloud` es viejo y rechaza `--autoclass-terminal-storage-class`, el
> bucket **igual queda bien**: sin esa bandera Autoclass baja hasta Nearline en
> vez de hasta Archive. Actualizá `gcloud` y volvé a correr ese `update`, o
> ponelo en la consola (Bucket → Configuración → Autoclass).

Las banderas del `create`, una por una:

| Bandera | Por qué |
|---|---|
| `--location us-east1` | Misma región que Cloud Run → tráfico gratis. **Irreversible.** |
| `--uniform-bucket-level-access` | Permisos sólo por IAM, sin ACLs por objeto. Es lo que espera `GS_DEFAULT_ACL = None` en `settings.py`. |
| `--public-access-prevention` | Bloquea que alguien lo haga público por accidente. Los recibos se sirven por `/transactions/{id}/receipt/`, que exige la misma membresía que el resto del API: la URL de GCS no se expone nunca. |
| `--default-storage-class STANDARD` | Lo que se acaba de subir se lee seguido; abaratarlo desde el día cero sale más caro (ver abajo). |

#### Por qué Autoclass y no reglas de ciclo de vida

Las dos bajan de clase lo que no se toca. La diferencia está en lo que cobran
cuando te equivocás de predicción:

|  | Reglas de ciclo de vida | Autoclass |
|---|---|---|
| Costo de la función | $0 | $0.0025 por cada 1 000 objetos/mes (sólo los ≥ 128 KiB) |
| Leer algo que ya bajó de clase | **Cobra recuperación** ($0.01–$0.05/GiB) | **No cobra recuperación** |
| Borrar antes del mínimo de permanencia | **Cobra hasta el mínimo** (30/90/365 días según la clase) | **No cobra** |
| Volver a subir de clase lo que se empezó a usar | No lo hace | Automático |

Un recibo es exactamente el caso que hace perder plata a las reglas fijas: no
hay forma de saber cuándo alguien va a abrir el del año pasado (una garantía, un
reclamo, la declaración de renta), ni cuándo va a borrar la transacción. Con
reglas, **cada** una de esas dos cosas tiene multa. Con Autoclass, ninguna.

Con 20 000 recibos la comisión de Autoclass son **$0.05/mes**, contra los ~$0.40
que ahorra el bajar de clase esos mismos objetos. Paga sola y no hay nada que
mantener.

#### Permisos y variable

```bash
# Las dos service accounts (servicio y job) son la misma por defecto en Cloud Run.
SA="$(gcloud projects describe $(gcloud config get-value project) \
      --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member "serviceAccount:$SA" --role roles/storage.objectAdmin

gcloud run services update budget-api --region us-east1 \
  --update-env-vars "GS_BUCKET_NAME=$BUCKET"
```

El Job diario necesita la **misma** variable — está en el `--set-env-vars` de
§6.1. Sin ella el backup avisa en los logs y no hace nada.

#### Comprobar

```bash
gcloud storage buckets describe gs://$BUCKET \
  --format="yaml(location,storageClass,autoclass,iamConfiguration,lifecycle)"
```

Tiene que decir `location: US-EAST1`, `autoclass.enabled: true`,
`uniformBucketLevelAccess.enabled: true` y
`publicAccessPrevention: enforced`. Después, de punta a punta: subí un recibo
desde la app y corré el backup a mano
(`gcloud run jobs execute budget-cron --region us-east1 --wait`); tienen que
aparecer los dos:

```bash
gcloud storage ls -r gs://$BUCKET
```

#### Cuánto cuesta

Precios de `us-east1` al 19 sep 2026 (por GiB/mes): Standard `$0.020`,
Nearline `$0.010`, Coldline `$0.004`, Archive `$0.0012`. Las primeras **5 GiB
de Standard en regiones de EE.UU. son gratis para siempre**, igual que 5 000
operaciones de escritura y 50 000 de lectura al mes.

| Escenario | Qué hay guardado | Al mes |
|---|---|---|
| Hoy (vos probando) | unos pocos MB | **$0** (entra en la capa gratis) |
| 100 usuarios, 1 año | ~24 000 recibos (~48 GB) + 30 volcados | **~$0.30** |
| 1 000 usuarios, 1 año | ~240 000 recibos (~480 GB) + 30 volcados | **~$3.60** |

Supone 20 recibos por usuario al mes y **~2 MB por recibo**, que es lo que sube
la app hoy: `expo-image-picker` con `quality: 0.7` comprime pero **no
redimensiona**, así que lo que viaja es la foto a la resolución completa de la
cámara.

Ahí está la única palanca que mueve la aguja: **el tamaño del recibo.** Bajarlo
a ~1 600 px del lado largo (~250 KB, de sobra para leer un ticket, y también
para que lo lea Gemini) deja esas mismas cifras en **~$0.10 y ~$0.90**, y de
paso hace la subida 8× más rápida con datos móviles — que es lo que el usuario
nota. Necesita `expo-image-manipulator`, que hoy no es dependencia del front.

Los volcados de la base ni se notan: comprimidos, y Neon free topa en 0.5 GB.
Con recibos chicos la comisión de Autoclass pasa a ser el renglón más grande,
y aun así son centavos.

El egreso a Cloud Run no se cobra por estar en la misma región, y los recibos
nunca salen a internet desde GCS: los sirve el backend.

---

## 3. Frontend — Vercel o Cloudflare Pages

> **Estado en producción (20-sep-2026):** el front corre en **Cloudflare Pages**
> (proyecto `moneyapp-8jz`), no en Vercel. `money.wxlter.dev` es un CNAME hacia
> `moneyapp-8jz.pages.dev` en el DNS del registrar; no hizo falta mover la zona a
> Cloudflare. Variables del build: `EXPO_PUBLIC_API_URL` (la URL de Cloud Run con
> `/api/v1`), `EXPO_PUBLIC_SENTRY_DSN`, `EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID` y
> `NODE_VERSION=20`. Tiene que ser un proyecto de **Pages**, no un Worker: un Worker
> con dominio propio exige la zona en Cloudflare. Para comprobar un build, buscar la
> URL de la API en el bundle (`/_expo/static/js/web/entry-*.js`); si trae
> `localhost:8000`, faltó la variable. El CORS del backend sólo acepta
> `https://money.wxlter.dev`, así que el login no funciona en `*.pages.dev`.

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
`GS_BUCKET_NAME` (o `DB_BACKUP_BUCKET`) en el Job, si no avisa y no hace nada —
el bucket se crea en [§2.5](#25-bucket-de-gcs--recibos-y-backups-una-sola-vez).
Restaurar desde un volcado: `RUNBOOK.md` §9.

### 6.1 Cloud Run Job (una sola vez)

```bash
gcloud run jobs deploy budget-cron \
  --source . --region us-east1 \
  --max-retries 1 \
  --set-secrets "DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "DJANGO_DEBUG=False,RUN_MIGRATIONS=0,GS_BUCKET_NAME=budget-recibos-prod" \
  --command python \
  --args "manage.py,run_daily_tasks"

# El permiso sobre el bucket ya se dio en §2.5 (misma service account).

gcloud run jobs execute budget-cron --region us-east1 --wait   # probarlo a mano una vez
```

`--max-retries 1`: con el valor por defecto (3), si sólo falla el backup los
reintentos rehacen los pasos 1 a 4 (recurrentes, cierres, recordatorios) en vano.
Un reintento cubre un fallo transitorio de red sin repetir todo tres veces.

> **Un Job no se redespliega solo.** Este comando fija una imagen y ahí se
> queda, mientras la base sigue migrando en cada arranque del servicio. En
> cuanto una migración borra una columna, el job falla contra un esquema que ya
> no es el suyo — y como corre de noche y sin público, nadie se entera. Pasó:
> `budget-cron` estuvo seis días tirando `column
> transactions_categorybudget.month does not exist`, sin recurrentes, sin
> cierres y sin recordatorios.
>
> Por eso el workflow de §9 sincroniza la imagen de los jobs con la del
> servicio en cada deploy. Si desplegás a mano con `deploy-cloudrun.sh`,
> acordate de hacer lo mismo:
>
> ```bash
> gcloud run jobs update budget-cron --region us-east1 \
>   --image "$(gcloud run services describe budget-api --region us-east1 \
>       --format='value(spec.template.spec.containers[0].image)')"
> ```

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

### 6.4 Alerta cuando el job diario falla

Sin esto, un `budget-cron` caído sólo se ve si alguien abre la consola de Cloud
Run. El backup estuvo roto (`server version mismatch`) sin que nada avisara.
La política está versionada en `infra/alerta-job-fallido.json`; cómo crearla y
qué cuenta como fallo, en `infra/README.md`. Se crea **sin canal**, y el aviso
recién llega cuando le agregás uno:

```bash
# 1. Un canal (correo, por ejemplo) -- una sola vez
gcloud beta monitoring channels create \
  --display-name="Avisos budget" --type=email \
  --channel-labels=email_address=TU_CORREO

# 2. Ver su id y colgárselo a la política
gcloud beta monitoring channels list --format="table(name,displayName)"
gcloud alpha monitoring policies update POLITICA_ID \
  --add-notification-channels=CANAL_ID
```

`gcloud alpha/beta monitoring` pide instalar componentes; la alternativa sin
instalar nada es la API REST (`infra/README.md`) o la consola: Monitoring →
Alerting → *Edit notification channels*.

Un job que **no llegó a correr** (Scheduler roto) no dispara esta alerta: no hay
ejecución que falle. Eso se ve en `RUNBOOK.md` §5.

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
- **Cloud Storage**: 5 GiB de Standard gratis para siempre en regiones de
  EE.UU. Con Autoclass lo viejo baja solo de clase; el detalle y las cifras
  proyectadas están en §2.5.
- **Backups**: Neon free retiene ~24 h. El volcado diario a GCS (§6) cubre el
  resto por centavos.

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

Después del smoke test, el mismo workflow le pone a `budget-cron` y a
`budget-migrate` **la imagen que acaba de quedar en el servicio** — no
construye otra: es el mismo commit, y así el job y el servicio no pueden correr
código distinto (ver el recuadro de §6.1). Un job que no exista se omite.

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
