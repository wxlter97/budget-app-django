# Configuración pendiente — cosas ya implementadas que no funcionan hasta que las configures

> Inventario al 19 sep 2026. Todo lo de acá **ya está programado y probado**; lo que falta
> es una cuenta, una key, un DNS o unas filas en `/admin/`. Mientras falte, la función
> existe en el código pero no hace nada (en casi todos los casos a propósito: el default
> es "desactivado y sin romper nada", nunca "abierto por accidente").
>
> Las variables se describen en `.env.example`; dónde se fijan en producción, en `DEPLOY.md`.
> No puedo verificar desde acá el estado real de tu Cloud Run / Neon / Vercel, así que lo
> marcado **(verificar)** puede que ya lo tengas hecho.

## Prioridad 1 — pérdida de datos o función caída

- [x] **`GS_BUCKET_NAME` — adjuntos de recibos *y* backups de la base.** Vacío = los recibos se
      guardan en el disco local del contenedor. En Cloud Run eso significa que **se borran en
      cada deploy**. La función de adjuntar foto/PDF a una transacción (`Transaction.receipt`)
      ya está completa, cámara incluida. **Los pasos, con las banderas que hacen que el
      bucket salga barato y privado desde el día cero, están en `DEPLOY.md` §2.5**
      (región = la de Cloud Run, y esa es irreversible; Autoclass; sin acceso público).
      **Verificado el 19-sep-2026:** el job `budget-cron` tiene `GS_BUCKET_NAME` y el backup
      sube a `backups/db/`.
      La misma variable habilita el volcado diario de la base (`manage.py backup_database`, que
      corre solo al final del job diario y guarda en `backups/db/` del mismo bucket): sin ella
      el comando avisa y no hace nada, y el único respaldo son las ~24 h de historial de Neon.
      Lo avisa `common.W003` en cada arranque. Si preferís un bucket aparte para los backups
      — otra política de retención, otro acceso — está `DB_BACKUP_BUCKET`. Restaurar:
      `RUNBOOK.md` §9. El `pg_dump` de la imagen tiene que ser >= la versión de Neon (hoy 18,
      `ARG PG_CLIENT_MAJOR` del Dockerfile); subirla cuando Neon suba, o el backup falla con
      `server version mismatch` (RUNBOOK §9).
- [ ] **Tareas diarias en producción.** No hay Celery en prod: los recordatorios, las
      transacciones recurrentes y los insights de comportamiento nuevos corren por
      `manage.py run_daily_tasks` desde un Cloud Run Job disparado por Cloud Scheduler
      (`DEPLOY.md` §6.1 y §6.2). Si el Job/Scheduler no está creado, **nada de lo diario
      corre nunca** y no hay ningún error visible que te avise. **(verificar)**
      Segundo modo de falla, ya visto en producción: el Job **existe pero corre una imagen
      vieja**. Un Cloud Run Job no se redespliega solo, y la base sí sigue migrando; en cuanto
      una migración borra una columna, el job revienta contra un esquema que ya no es el suyo.
      `budget-cron` estuvo así seis días. Desde entonces el workflow de deploy le sincroniza la
      imagen al job en cada push a `main` (`DEPLOY.md` §9); si desplegás a mano, el recuadro de
      §6.1 tiene el comando.
- [ ] **`CACHE_URL` (Redis) en producción.** Vacío = el throttling de DRF cuenta en la memoria
      de cada instancia: con las 5 instancias que permite el servicio, los límites valen 5 veces
      y se reinician en cada deploy. Al arrancar el contenedor, el check `common.W001` lo avisa
      en los logs. Memorystore son ~$35/mes de más: alcanza Redis de Upstash o la tabla de cache
      de Django en Postgres.
      **Hoy no hace falta:** producción corre con `maxScale: 1` (verificado el 19-sep-2026), o
      sea una sola instancia, y ahí el throttling en memoria cuenta bien. Lo único que se pierde
      es la cuenta en cada deploy o al escalar a cero. Pasa a ser necesario **el día que se suba
      el máximo de instancias**; hasta entonces `common.W001` es ruido. `CACHE_URL` lleva la URL
      completa con contraseña: va como secreto (`--set-secrets CACHE_URL=cache-url:latest`),
      nunca como variable en claro.
- [ ] **Endpoint *pooled* de Neon en `DATABASE_URL`.** Con `DJANGO_DB_CONN_MAX_AGE=0` se abre una
      conexión por request; contra el endpoint directo, las conexiones topan antes que el CPU en
      cuanto hay más de una instancia. Host con `-pooler` +
      `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True`; el check `common.W002` lo avisa. El host
      ya es `-pooler` en producción (verificado el 19-sep-2026); **(verificar)** sólo la variable
      de cursores.
- [ ] **Alerta cuando el job diario falla.** Hoy un `budget-cron` caído sólo se ve si alguien
      abre la consola de Cloud Run: el backup estuvo roto (`pg_dump` 17 contra Neon 18.6) sin
      que nada avisara. Una alerta de Cloud Monitoring sobre ejecuciones fallidas del job, al
      mismo canal de `SUPPORT_WEBHOOK_URL`. De paso, con los 3 reintentos por defecto, si falla
      sólo el backup se rehacen los pasos 1 a 4 en vano: `--max-retries` más bajo, o el backup
      en su propio job (ROADMAP 0.13 y 0.14).

## Prioridad 2 — funciones completas que hoy no se pueden usar

- [ ] **`GEMINI_API_KEY` — todas las funciones de IA.** Vacía = la IA queda apagada de punta a
      punta: `GET /api/v1/ai/status/` responde `enabled: false` y el front no muestra ninguna
      entrada de IA (por diseño, así no aparecen botones que fallan al tocarlos). La base ya
      está: cliente, throttle, cuota mensual por plan y registro de consumo (`apps/ai/`).
      **Usar el tier de pago, no la capa gratis:** la gratis usa los datos para entrenar, así
      que sólo sirve para probar con datos propios.
      Las cuotas por plan no son variables de entorno — viven en `Plan.features` y las siembra
      `manage.py seed_billing_plans` (Free 3 recibos/10 parseos/0 chats · Plus 30/50/20 ·
      Pro 100/200/100). Se pueden ajustar desde `/admin/` sin deploy.
      **Estado al 20-sep-2026:** la key ya está en el servicio como el secreto `gemini-api-key`
      y las cuotas por plan quedaron sembradas. La IA está **apagada a propósito** desde el admin
      (*Common → Module flags → `ai`*) mientras se definen precios; ese interruptor corta el
      gasto en el servidor (`services.run()` lo consulta), no sólo esconde los botones. Para
      volver a encenderla hace falta **crédito de prepago** en AI Studio: sin saldo la API
      responde 402 (*prepayment credits are depleted*), y con el saldo agotado también corta
      el gasto. Los modelos son de la serie 3 (`apps/ai/pricing.py`): los 2.5 devuelven 404 para
      cuentas nuevas aunque figuren en la lista de modelos. Al encender la IA, mergear el
      borrador de la política de privacidad (`moneyapp`, sección "Inteligencia artificial").
      **Ojo con `budget-admin`:** su imagen no se sincronizaba con los deploys, así que la
      primera vez sembró los planes *sin* las claves de IA; ya se sincroniza (ver
      `deploy.yml`).
- [ ] **Importación por correo bancario.** Necesita tres cosas y hoy no anda sin ellas:
      1. `INBOUND_EMAIL_DOMAIN` + ruta *inbound* en Mailgun (o similar) apuntando al webhook,
         con sus registros MX en el DNS.
      2. `INBOUND_WEBHOOK_SECRET` (o `INBOUND_MAILGUN_SIGNING_KEY`). **Vacío = el endpoint
         rechaza todo**, a propósito.
      3. **Filas de `BankEmailSchema`** cargadas a mano en `/admin/` (es catálogo global,
         sólo staff lo edita, y no hay fixtures). Sin el schema del banco, cada correo
         importado termina en `EmailImportLog` con estado `failed`.
- [ ] **Correo saliente (invitaciones a workspace).** `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD`
      de un *sending domain* verificado en Mailgun, más `DJANGO_DEFAULT_FROM_EMAIL` y
      `INVITE_ACCEPT_URL_BASE`. Sin esto, en `DEBUG` los correos sólo se imprimen en consola
      y en producción las invitaciones no llegan (compartir workspace queda inservible).
      **Estado al 20-sep-2026:** configurado en el servicio con el dominio `inbound.wxlter.dev`
      (Mailgun sólo permite un dominio; SPF, DKIM y tracking ya estaban publicados). La
      contraseña SMTP va en el secreto `mailgun-smtp-password` e `INVITE_ACCEPT_URL_BASE` es
      `https://money.wxlter.dev/invite`. **(verificar)** con una invitación real: falta la prueba
      de punta a punta.
- [ ] **Catálogo de programas de lealtad.** `Bank`, `CardProduct`, `LoyaltyProgram` y
      `LoyaltyCategoryRate` son catálogo global editable sólo por staff, y no hay fixtures.
      Además cada `Category` del workspace tiene que quedar mapeada a un `CategoryType` para
      heredar la tasa. Sin cargar nada: los puntos, el cashback y el descuento sugerido nunca
      se calculan, aunque toda la lógica y la UI ya estén.
- [ ] **Pagos y suscripciones (Wompi).** `WOMPI_API_KEY` + `WOMPI_WEBHOOK_SECRET`, más
      `manage.py seed_billing_plans` una sola vez (`DEPLOY.md` §2.4) y
      `manage.py grandfather_existing_users` si ya hay usuarios reales de antes. Sin esto no
      se puede cobrar y los planes quedan sin precio. Ojo con el webhook: si deja de llegar,
      nadie se activa después de pagar (`RUNBOOK.md` §6).
- [ ] **"Continuar con Google".** `GOOGLE_CLIENT_IDS` en el backend **y**
      `EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID` / `_ANDROID_` / `_WEB_` en el front. Hace falta un
      OAuth client por plataforma en Google Cloud Console. Sin ninguno configurado, el botón
      de Google directamente no se muestra (no queda un botón roto).

## Prioridad 3 — notificaciones push

El centro de notificaciones dentro de la app funciona sin nada de esto; lo que falta es el
aviso *fuera* de la app.

- [ ] **Push en navegador (VAPID).** `manage.py generate_vapid_keys` y pegar
      `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT`. Vacío = se omiten los pushes
      a navegadores. Es el que más rinde hoy, porque la app en producción es la web.
      **Estado al 19-sep-2026:** las tres variables ya están en el *servicio* (desde el 14-sep,
      la privada como secreto `vapid-private-key`) pero **no en el job `budget-cron`**, que es
      quien manda los recordatorios diarios: por eso sus logs dicen "Push web sin VAPID
      configurado". Falta pasárselas al job (`gcloud run jobs update budget-cron
      --update-env-vars VAPID_PUBLIC_KEY=…,VAPID_SUBJECT=… --update-secrets
      VAPID_PRIVATE_KEY=vapid-private-key:latest`).
- [ ] **Push nativo (iOS/Android).** Falta `extra.eas.projectId` en el `app.json` de `moneyapp`
      (correr `eas init`), y después las credenciales de FCM para Android y de APNs para iOS
      (esto último requiere cuenta de Apple Developer, ~$99/año). Sin el `projectId` la app
      loguea un aviso y no registra el token — no hay build nativo publicado todavía, así que
      esto va junto con la primera subida a tiendas.

## Prioridad 4 — visibilidad y cosas menores

- [ ] **Sentry.** `SENTRY_DSN` en el backend y `EXPO_PUBLIC_SENTRY_DSN` en el front. Vacío =
      ni se importa el SDK. Hasta que esté, los errores de producción sólo se ven si un
      usuario te los cuenta. **Estado al 19-sep-2026:** `SENTRY_DSN` ya está en el servicio
      (secreto `sentry-dsn`, desde el 14-sep) pero no en el job `budget-cron`, así que los
      fallos del job diario no llegan a Sentry. Se le pasa con `--update-secrets
      SENTRY_DSN=sentry-dsn:latest`. Falta confirmar `EXPO_PUBLIC_SENTRY_DSN` en el front.
- [ ] **Avisos de tickets de soporte.** `SUPPORT_WEBHOOK_URL` (webhook de un canal de Discord).
      Vacío = los tickets se guardan en la base pero nadie te avisa que entraron.
      **Estado al 20-sep-2026:** `SUPPORT_WEBHOOK_URL` ya está en el servicio como el secreto
      `support-webhook-url`. **(verificar)** creando un ticket de prueba desde la app.
- [ ] **Dashboard externo (Villa Wxlter).** `DASHBOARD_API_TOKEN` y `DASHBOARD_WORKSPACE_ID`.
      Vacío = el endpoint `GET /api/v1/dashboard/balance/` rechaza todo.
- [ ] **Atajo de Apple Shortcuts.** `EXPO_PUBLIC_SHORTCUT_URL`: hay que armar el Atajo una vez
      en un iPhone y copiar su link de iCloud. No se puede generar desde el backend (iOS sólo
      importa `.shortcut` firmados y firmar requiere macOS). Sin el link, Herramientas → Atajos
      sólo muestra las instrucciones para armarlo a mano.
- [ ] **Deploy automático al mergear a `main`.** GitHub Actions + Workload Identity Federation
      para el backend y la integración de Git de Vercel para el front (`DEPLOY.md` §9).
      **(verificar)**

## No es configuración tuya, pero conviene saberlo

- **Tasas de cambio.** `ExchangeRate` se carga a mano por workspace (Herramientas → Monedas en
  la app); no hay API externa. Una transacción en una moneda sin tasa se **excluye** de los
  totales de reportes en vez de contarse mal.
- **Categorías por defecto.** Se siembran solas al crear el workspace. `manage.py seed_categories`
  sólo hace falta para workspaces creados antes de ese cambio.
- **Modo mantenimiento.** `MAINTENANCE_MODE=True` deja todo el API en 503 salvo `/healthz/` y
  `/admin/`, y el front muestra pantalla de mantenimiento (`RUNBOOK.md` §8). No hay que
  configurar nada de antemano, sólo recordar que existe.
