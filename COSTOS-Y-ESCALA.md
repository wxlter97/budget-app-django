# Costos y escala — qué aguanta el stack actual y cuánto cuesta por usuario

> Calculado el 18 sep 2026 con los precios públicos de esa fecha. Los costos de IA (Gemini)
> viven aparte, en `moneyapp/docs/backlog-nuevas-funciones.md` → "Costos de IA y topes por plan",
> porque todavía no están implementados.
>
> Precios de referencia usados acá: **Cloud Run** `$0.000024`/vCPU-s + `$0.0000025`/GiB-s +
> `$0.40`/millón de requests, con capa gratis mensual de 180 000 vCPU-s / 360 000 GiB-s /
> 2 M requests. **Neon** free = 0.5 GB + 100 horas de cómputo al mes, auto-suspende a los 5 min;
> Launch = `$0.106`/CU-hora + `$0.35`/GB-mes, sin mínimo mensual. **Cloud Scheduler** 3 jobs
> gratis por cuenta, después `$0.10`/job/mes. **Vercel** Hobby gratis pero **prohibido para uso
> comercial**; Pro `$20`/mes con 1 TB de banda. **Cloud Storage** ~`$0.02`/GB-mes.

## Veredicto corto

**La arquitectura es la correcta y no hay que migrar nada.** Escalar a cero (Cloud Run) más
Postgres serverless (Neon) es exactamente lo que le conviene a un producto que va a tener
decenas o cientos de usuarios y ratos enteros sin tráfico: si nadie lo usa, no cuesta. Pasarlo a
VMs o Kubernetes sería más caro y más trabajo para el mismo resultado.

Lo que **no** está listo para varios usuarios no es el stack, son cinco parámetros y un job. Todo
está en la lista de abajo y ninguno es una reescritura.

## Lo que hay que arreglar antes de tener usuarios de verdad

Ordenado por lo que rompe primero. Lo marcado `[x]` ya está hecho en el repo; lo que sigue en
`[ ]` es acción de configuración tuya (ver también `CONFIG-PENDIENTE.md`).

- [x] **`--max-instances 1` en `deploy-cloudrun.sh`.** Con `--concurrency 8`, el backend entero
      tenía un techo de **8 requests en vuelo** y una sola instancia; la novena esperaba. Y cada
      deploy era caída para todos, no para una fracción. **Ahora `--max-instances 5`** (techo de
      40 en vuelo), que no cambia el costo: Cloud Run cobra por uso, no por instancia
      disponible.
      *Contrapartida anotada en `entrypoint.sh`:* las migraciones corren al arrancar cada
      contenedor, así que con varias instancias un release con migraciones puede dar unos 502
      mientras la que perdió la carrera reintenta. El Job `budget-migrate` + `RUN_MIGRATIONS=0`
      lo elimina y además acelera el arranque en frío (`DEPLOY.md` §2.2).
- [x] **Avisos de arranque en vez de fallas silenciosas.** `apps/common/checks.py` agrega dos
      checks de `--deploy` que `entrypoint.sh` corre al iniciar el contenedor, así que el aviso
      queda en los logs de Cloud Run con las variables reales: `common.W001` si el cache es de
      proceso (throttling no compartido) y `common.W002` si `DATABASE_URL` apunta al endpoint
      directo de Neon en vez del pooled. Nunca bloquean el arranque.
- [ ] **`CACHE_URL` vacío** (config tuya). El throttling de DRF cuenta en la memoria de cada
      instancia. Con una sola instancia "funcionaba" por accidente; con 5 los límites valen 5
      veces y se reinician en cada deploy. Memorystore son ~$35/mes por algo que no los vale
      acá: Redis de Upstash (cobra por request, gratis en volumen bajo) o la tabla de cache de
      Django en Postgres. `common.W001` te lo va a recordar en cada arranque.
- [ ] **Endpoint pooled de Neon** (config tuya). `DJANGO_DB_CONN_MAX_AGE=0` abre una conexión por
      request; contra el endpoint directo, el límite de conexiones se alcanza antes que el CPU
      en cuanto hay más de una instancia. Usar el host con `-pooler` y
      `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True`. `common.W002` te lo avisa.
- [x] **El job diario hacía 7× el trabajo que necesita.** `notify_insights()` llamaba a
      `behavior_insights()` (~12 queries por membresía) **todos los días**, aunque el aviso sea
      semanal: la `dedupe_key` evitaba la notificación repetida, no el cómputo. Ahora se calcula
      un solo día de la semana (`services.INSIGHTS_WEEKDAY`, lunes). Con 1 000 usuarios son
      ~72 000 queries menos por semana.
- [x] **N+1 en el mismo job.** `_get_preference(user)` y `_devices_for(user)` se consultaban una
      vez por membresía. Ahora se consultan una vez por usuario: quien esté en 3 workspaces las
      paga una sola vez.
- [ ] **Mover el front fuera de Vercel Hobby** (acción tuya, ver abajo). Hobby no permite uso
      comercial: desde que cobrás una suscripción estás fuera de los términos.
- [x] **Backups.** Neon free retiene ~24 h de historial, que con usuarios reales no alcanza.
      `manage.py backup_database` vuelca con `pg_dump --format=custom` y sube a
      `gs://<bucket>/backups/db/`, con retención de 30 días que nunca borra el último volcado
      que queda. Corre solo al final de `run_daily_tasks`. Restaurar está paso a paso en
      `RUNBOOK.md` §9. **Falta la config tuya:** sin `GS_BUCKET_NAME` (o `DB_BACKUP_BUCKET`) el
      comando avisa y no hace nada, y `common.W003` lo repite en cada arranque.
- [x] **`DEPLOY.md` §7 decía "Neon free: ~190 h cómputo/mes";** hoy son 100 h.

## Hosting del front: qué conviene

El front es un build **estático** (`expo export -p web`, `web.output: "single"`, sin SSR ni
funciones de servidor), así que cualquier CDN sirve y la decisión es sólo de precio y términos.

| Opción | Costo | Banda | Uso comercial | Nota |
|---|---|---|---|---|
| **Cloudflare Pages** | $0 | **ilimitada** (assets estáticos) | **sí** | 500 builds/mes. La recomendada. |
| Vercel Pro | $20/mes | 1 TB, después $0.15/GB | sí | Sólo si querés quedarte con la DX de Vercel. |
| Vercel Hobby | $0 | 100 GB | **no** | Lo que hay hoy. Fuera de términos al cobrar. |
| Netlify Free | $0 | 100 GB | sí | Techo bajo de banda. |
| GCS + Cloud CDN | ~$0.12/GB de egreso | pago por uso | sí | Mismo proyecto GCP, pero es la más cara de la lista. |

**Cloudflare Pages es la mejor opción:** es la única con banda ilimitada, no tiene la restricción
comercial, y da preview deployments por PR y deploy en cada push a `main` igual que Vercel. Los
pasos están en `DEPLOY.md` §3, Opción C.

Dos cosas que además se ganan, no sólo el precio: Cloudflare tiene 300+ puntos de presencia
contra ~30 regiones de Vercel, lo que en Centroamérica se nota; y **Cloudflare Web Analytics es
gratis y sin cookies**, así que sirve donde GA4 y Clarity chocan con la política de privacidad
(ver el punto 4 del backlog de funciones nuevas).

### Los peros, para que la decisión sea con los ojos abiertos

- **DX más austera.** Logs de build, rollback y preview son más pulidos en Vercel. No cambia lo
  que ve el usuario, pero se siente al operar.
- **Puerta de un solo sentido si algún día hace falta código en el servidor.** Las Pages
  Functions corren en el runtime de Workers, que no es Node: hay paquetes de npm que no andan.
  Hoy da igual (el backend es Django y el front no necesita nada de servidor), pero es el
  único riesgo estructural de la mudanza.
- **DNS.** Lo más cómodo es tener la zona `wxlter.dev` en Cloudflare. Se puede apuntar con un
  CNAME desde otro proveedor, pero es menos directo — **confirmar dónde está la zona hoy antes
  de arrancar.**
- **Un build concurrente y 500 builds/mes en el plan gratis** (Vercel Hobby también da uno
  concurrente). A este ritmo de push no molesta; con muchas ramas a la vez se forma cola.
- **La banda ilimitada no es un cheque en blanco:** los términos del plan gratis apuntan contra
  usar el CDN para servir mucho contenido que no sea del sitio (video, archivos). Un SPA de
  ~2 MB con 7 assets es exactamente el caso para el que está pensado.
- **Concentración de proveedor.** Si terminás con DNS + hosting + analítica en Cloudflare, una
  caída de ellos te toca todo junto.
- **`_headers` no aplica a respuestas de Functions** (irrelevante mientras no haya Functions) y
  admite hasta 100 reglas, de sobra para cache-control y CSP.

**Si la mudanza no vale el rato:** Vercel Pro a $20/mes es defendible y es cero riesgo. Son ~20
suscriptores de Plus al mes, así que a partir de unos cientos de usuarios deja de ser un rubro
que importe.

## Arranque en frío (lo que el usuario sí va a notar)

Cloud Run en 0 instancias más Neon auto-suspendido a los 5 minutos significa que la primera
request después de un rato inactivo paga el arranque de Django **más** el despertar de la base.

- **No** poner `--min-instances 1` por reflejo: una instancia siempre viva es del orden de
  **$50–70/mes** (2.6 M vCPU-s + 2.6 M GiB-s al mes, menos la capa gratis), y no arregla el
  despertar de Neon.
- Mucho más barato: un **ping del Cloud Scheduler cada 5–10 minutos en horario activo** a
  `/healthz/`, que mantiene vivas las dos cosas por centavos. Es el mismo Scheduler que ya
  disparás para las tareas diarias y sigue entrando en los 3 jobs gratis.

## Costo por usuario

**Costo marginal de un usuario activo** (el que se suma por cada persona nueva):

| Recurso | Supuesto | Costo/usuario/mes |
|---|---|---|
| Cloud Run | ~500 requests/mes, ~150 ms de CPU cada uno | ~$0.002 |
| Cloud Storage (recibos) | 10 recibos/mes de ~300 KB, acumulando | ~$0.001 |
| Neon (cómputo y storage) | se reparte, no escala lineal | <$0.01 |
| Correo transaccional | sólo invitaciones, volumen bajo | <$0.01 |
| **Marginal total** | | **~1 ¢** |

Es decir: **la infraestructura por usuario es despreciable; lo que cuesta es el piso fijo.** Y la
capa gratis es grande — 180 000 vCPU-s alcanzan para del orden de **2 000 usuarios activos** al
mes antes de pagar un centavo de Cloud Run.

**Costo total según cuántos usuarios haya** (infra + IA de un perfil medio, ~3 ¢):

| Usuarios activos | Infra/mes | Infra por usuario | Total por usuario |
|---|---|---|---|
| 10 | ~$20–25 (Vercel Pro + Neon free) | ~$2.20 | ~$2.25 |
| 100 | ~$25–45 (Neon empieza a cobrar) | ~$0.35 | ~$0.38 |
| 1 000 | ~$60–90 | ~$0.08 | ~$0.11 |
| 10 000 | ~$300–500 (Neon Scale, Cloud Run sobre la capa gratis) | ~$0.04 | ~$0.07 |

**Qué empieza a cobrar primero, en orden:** Vercel (en el momento en que cobrás, por los
términos) → Neon cómputo (cuando hay actividad repartida en el día y la base deja de
auto-suspenderse, del orden de 20–30 usuarios activos) → Neon storage (pasados los 0.5 GB) →
Cloud Run (recién arriba de ~2 000 usuarios activos).

## Lo que esto dice del precio de los planes

A **$0.99/mes**, con un neto de ~$0.65 después de la comisión del procesador, el piso fijo de
~$25/mes necesita del orden de **40 suscriptores pagos sólo para empatar la infraestructura**, y
~100 para que el piso deje de ser el rubro más grande. La IA no mueve esa aguja (1–12 ¢ por
usuario): la mueven el piso fijo y la comisión por cobrar.

Dos consecuencias prácticas: **empujar el plan anual** (la comisión fija pesa ~6% en $9.99 contra
~30% en $0.99 mensual) y **mover el front a Cloudflare Pages** para sacarse los $20/mes de
Vercel, que a esta escala es el rubro más grande de todos.
