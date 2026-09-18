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

Ordenado por lo que rompe primero.

- [ ] **`--max-instances 1` en `deploy-cloudrun.sh`.** Con `--concurrency 8`, el backend entero
      tiene un techo de **8 requests en vuelo** y una sola instancia; la novena espera. Y cada
      deploy es caída para todos, no para una fracción. Subirlo a 3–5 no cambia el costo (se
      paga por uso, no por instancia disponible) y saca el techo.
- [ ] **`CACHE_URL` vacío.** El throttling de DRF cuenta en la memoria de cada instancia. Con una
      sola instancia "funciona" por accidente; con 3 instancias los límites valen el triple y se
      reinician en cada deploy. Memorystore son ~$35/mes por algo que no los vale acá: usar
      Redis de Upstash (cobra por request, gratis en volumen bajo) o incluso la tabla de cache de
      Django en Postgres.
- [ ] **Conexiones a Neon.** `DJANGO_DB_CONN_MAX_AGE=0` abre una conexión por request. Con varias
      instancias, el límite de conexiones de Neon se alcanza antes que el CPU. Usar el endpoint
      **pooled** (`-pooler` en el host) y `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True` — está en
      `DEPLOY.md`, falta confirmar que el `DATABASE_URL` de producción es el pooled.
- [ ] **El job diario es O(usuarios) y hace 7× el trabajo que necesita.** `notify_insights()`
      recorre las membresías activas y llama a `behavior_insights()` **todos los días**, aunque
      el aviso sea semanal: la `dedupe_key` evita la notificación repetida, no el cómputo. Son
      ~10–15 queries por membresía por día, así que con 1 000 usuarios son ~12 000 queries
      diarias que se tiran a la basura 6 de cada 7 días. Arreglo barato: calcular sólo el día
      objetivo de la semana, o consultar `NotificationLog` **antes** de calcular.
- [ ] **N+1 en el mismo job:** `_get_preference(user)` y `_devices_for(user)` se consultan una vez
      por membresía, no una vez por usuario. Quien esté en 3 workspaces las paga 3 veces.
- [ ] **Vercel Hobby no permite uso comercial.** Desde el momento en que cobrás una suscripción,
      el front en Hobby está fuera de los términos. Dos salidas: Vercel Pro ($20/mes) o mover el
      build web a **Cloudflare Pages**, cuyo plan gratis sí permite uso comercial. Es un build
      estático (`web.output: "single"`), así que mudarlo es casi sólo apuntar el DNS.
- [ ] **Backups.** Neon free retiene ~24 h de historial. Con usuarios reales eso no alcanza: un
      `pg_dump` a un bucket de GCS desde el mismo job diario cuesta centavos.
- [ ] **`DEPLOY.md` §7 está desactualizado:** dice "Neon free: ~190 h cómputo/mes"; hoy son 100 h.

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
