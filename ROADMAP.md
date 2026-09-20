# Roadmap — de acá a producción, y después

> Escrito el 18 sep 2026. Es el índice maestro: junta lo que falta para salir a producción de
> verdad con las funciones nuevas que ya están diseñadas. Los detalles viven en los documentos
> que cada punto referencia, no acá — este archivo es para saber **qué sigue y en qué orden**.
>
> **Quién hace qué:** 🧑 = acción tuya (crear una cuenta, pegar una key, mover un DNS); 🤖 =
> código, que se hace en sesión. Los tiempos 🤖 son de trabajo efectivo, no de calendario.
>
> Documentos de referencia:
> - `CONFIG-PENDIENTE.md` — lo ya implementado que espera configuración.
> - `COSTOS-Y-ESCALA.md` — capacidad, costo por usuario y hosting.
> - `DEPLOY.md` / `RUNBOOK.md` — cómo se despliega y qué hacer cuando se rompe.
> - `moneyapp/docs/backlog-nuevas-funciones.md` — diseño de las funciones nuevas y costos de IA.
> - `moneyapp/docs/audit-tasks.md` — backlog de la auditoría de producto (36 de 38 cerrados).

## Dónde estamos hoy

- Los PRs #55 y #56 (backend) y #74 y #75 (front) **están mergeados a `main`**. El código de
  producto está al día: reembolsos, división entre personas, Personas, ahorro con interés,
  gamificación, insights de comportamiento, y los arreglos de escala del backend.
- Además ya están **el backup diario a GCS (0.8)**, **el `_redirects` para Cloudflare (0.5)**,
  **la base de IA con su cuota por plan (2.1)**, **el escaneo de recibos (2.2)** y **la entrada
  por texto libre (2.3)**. Todas esperan configuración tuya: el bucket, el DNS y la key de
  Gemini.
- La app corre en web (Vercel Hobby). **No hay build nativo publicado** y no hay
  `extra.eas.projectId`.
- 850 tests en el backend, 257 en el front, todos pasando.
- Lo que falta no es código de producto: es configuración, cobrar, y las funciones nuevas.

---

## Fase 0 — Producción sólida (antes de invitar a nadie)

Esto va primero porque construir funciones encima de una instalación que pierde los recibos y no
corre las tareas diarias es tirar trabajo. Casi todo es 🧑.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 0.1 | **`GS_BUCKET_NAME`** — sin esto los recibos se borran en cada deploy | 🧑 | 20 min |
| 0.2 | **Cloud Scheduler + Job `budget-cron`** — sin esto no corre nada diario (recordatorios, recurrentes, insights) y no hay error que avise | 🧑 | 30 min |
| 0.3 | ~~**Endpoint *pooled* de Neon** en `DATABASE_URL`~~ — **hecho** (verificado el 20-sep-2026): el host es `-pooler` y el servicio y el job tienen `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True` | 🧑 ✅ | — |
| 0.4 | **`CACHE_URL`** con Redis de Upstash — **diferido por decisión** (20-sep-2026): el throttling en memoria cuenta bien con una sola instancia (`maxScale: 1`, verificado el 19-sep) y sólo se reinicia en cada deploy o arranque en frío. Reabrir el día que se suba el máximo de instancias. Ojo: con Redis la API pasa a depender de un servicio externo, porque el backend de Redis de Django no falla en abierto | 🧑 | 30 min |
| 0.5 | ~~**Mover el front a Cloudflare Pages**~~ — **hecho** (20-sep-2026): proyecto de Pages `moneyapp-8jz`; `money.wxlter.dev` apunta por CNAME y la zona DNS no se movió del registrar. El despliegue de Vercel quedó pausado y su integración Git ya está desconectada (`DEPLOY.md` §3) | 🧑 ✅ | — |
| 0.6 | ~~**Job `budget-migrate` + `RUN_MIGRATIONS=0`**~~ — **hecho** (20-sep-2026): el job existe, el servicio tiene `RUN_MIGRATIONS=0` y `deploy.yml` migra antes de mover el tráfico (`DEPLOY.md` §2.2). Regla que trae: nunca borrar ni renombrar una columna en el mismo release que deja de usarla | 🧑 ✅ | — |
| 0.7 | **Sentry** en los dos repos — hasta que esté, los errores de producción sólo se ven si alguien los cuenta | 🧑 | 30 min |
| 0.8 | ~~**Backups**: `pg_dump` a GCS desde el job diario~~ — **hecho**: `manage.py backup_database` corre al final de `run_daily_tasks` y `RUNBOOK.md` §9 tiene la restauración. Se activa solo cuando exista el bucket de 0.1. Ojo: hasta el 19-sep-2026 no produjo ni un volcado, porque la imagen traía `pg_dump` 17 y Neon corre 18.6 (`server version mismatch`); arreglado instalando el cliente desde PGDG (`ARG PG_CLIENT_MAJOR` del Dockerfile) | 🤖 ✅ | — |
| 0.9 | ~~**Ping de keepalive** del Scheduler~~ — **hecho** (20-sep-2026): job `budget-keepalive`, cada 5 min de 06:00 a 01:55 hora de El Salvador (elegido con el tráfico real: nada entre las 02h y las 05h). Sólo calienta Cloud Run; `/healthz/` no toca Neon | 🧑 ✅ | — |
| 0.10 | ~~**Correo saliente (Mailgun)**~~ — **hecho** (20-sep-2026): configurado con `inbound.wxlter.dev` (el plan de Mailgun sólo permite un dominio) y probada una invitación de punta a punta | 🧑 ✅ | — |
| 0.11 | ~~**Push web (VAPID)**~~ — **hecho** (20-sep-2026): las claves estaban en el servicio desde el 14-sep y ahora también en el job `budget-cron`, que es quien manda los recordatorios diarios | 🧑 ✅ | — |
| 0.12 | ~~**`SUPPORT_WEBHOOK_URL`** (Discord)~~ — **hecho** (20-sep-2026): secreto `support-webhook-url`, probado con un ticket real | 🧑 ✅ | — |
| 0.13 | ~~**Alerta de fallo del job `budget-cron`**~~ — **hecho** (20-sep-2026): política `budget-cron: ejecución fallida` (`infra/alerta-job-fallido.json`) con canal de correo. Confirmar en Monitoring que el canal figura como verificado | 🧑 ✅ | — |
| 0.14 | ~~**Reintentos del job diario**~~ — **hecho** (20-sep-2026): `--max-retries 1` en `budget-cron` (antes 3: si fallaba sólo el backup se rehacían los pasos 1 a 4 en vano) | 🧑 ✅ | — |

**Subtotal: ~1.5 jornadas**, casi todas tuyas. 0.1 a 0.5 son el mínimo real para abrir.

---

## Fase 1 — Antes de cobrar

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 1.1 | **Wompi**: cuenta creada. **Hallazgo (20-sep):** `docs.wompi.sv` **es público** (el esqueleto de `WompiProvider` se escribió creyendo lo contrario). Lo que documenta: OAuth 2.0 *client credentials* (`id.wompi.sv/connect/token`, `client_id` = App ID y `client_secret` = API Secret del negocio); **Enlace de Pago** (`POST /EnlacePago`, con `identificadorEnlaceComercio` que vuelve en el webhook y `urlWebhook`/`urlRedirect` por enlace); webhook firmado con `wompi_hash` = HMAC-SHA256 del cuerpo crudo con el API Secret, y **sólo notifica cobros exitosos**; negocio en *modo desarrollo* para probar (CVV `111` simula rechazo). **Cobros recurrentes:** existe `EnlacePagoRecurrente`, pero es un enlace por plan (monto y día fijos) que cada cliente acepta a mano, no acepta una referencia nuestra y su único endpoint de baja desactiva el enlace entero; sin confirmar cómo identificar a cada suscriptor ni cancelar uno solo. **Pendiente de Wompi:** la tarifa (no es pública) y esas dos respuestas | 🧑 | 1 h |
| 1.2 | `manage.py seed_billing_plans` + `grandfather_existing_users` si ya hay usuarios | 🧑 | 15 min |
| 1.3 | **Probar el flujo completo de punta a punta**: trial → cobro → webhook → activación → cancelación. **Bloqueado hasta implementar `WompiProvider` contra el sandbox de Wompi** (ver 1.1) | 🤖 + 🧑 | 2–3 h |
| 1.4 | **Revisar precios a la luz del costo real.** Empujar el anual; decidir si el lifetime de $19.99 se mantiene (con IA es ~14 años de consumo para empatar). Análisis con los números: `ECONOMIA-POR-PLAN.md` (se regenera con `scripts/economia_por_plan.py`) | 🧑 | decisión |
| 1.5 | **Legal**: revisar `privacy.tsx` y términos contra lo que de verdad va a hacer la app (IA, analítica, terceros). Hoy la política promete que no hay rastreadores de terceros | 🤖 + 🧑 | 2 h |
| 1.6 | **Catálogo de lealtad** (`Bank`, `CardProduct`, `LoyaltyProgram`, tasas) y mapear categorías a `CategoryType` — sin filas, puntos y cashback nunca se calculan. **Hecho en código (20-sep):** `seed_loyalty_catalog` (`DEPLOY.md` §2.3b) con Agrícola, BAC, Promérica, Azul, Atlántida, Hipotecario y lo verificable de Cuscatlán e Industrial, tasas por **día de la semana**, **comercios** (`Merchant`: tasas de un solo comercio y rubro más fino que la categoría) y `map_categories_to_rubros`. **Falta:** correr los dos comandos en producción y completar Cuscatlán (MultiPuntos), Davivienda, Industrial, ABANK y Apoyo Integral (sus sitios no publican la tasa) | 🧑 | 1 h |
| 1.7 | **Schemas de correo bancario** (`BankEmailSchema`), uno por banco; sin ellos toda importación falla | 🤖 + 🧑 | 1–2 h por banco |

**Subtotal: ~1.5–2 jornadas** (más lo que sume cada banco).

---

## Fase 2 — Funciones nuevas (el trabajo grande)

Diseño completo, decisiones y costos: `moneyapp/docs/backlog-nuevas-funciones.md`.
Orden pensado para que cada pieza apoye la siguiente.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 2.1 | ~~**Base de IA** (`apps/ai`)~~ — **hecha** y con la key ya en el servicio (secreto `gemini-api-key`, 20-sep-2026): cliente de Gemini, throttle `ai`, cuota mensual por plan (`Plan.features`, fail-closed), `AIUsage` como log y contador a la vez, `GET /ai/status/` y `useAIStatus()` en el front. **Estado:** la IA está **apagada a propósito** desde el admin (*Common → Interruptores de módulos → `ai`*) mientras se definen precios, y ese interruptor ahora corta el gasto también en el servidor. **Para volver a encenderla:** (1) crédito de prepago en AI Studio (sin él la API responde 402 *prepayment credits are depleted*), (2) prender el interruptor, (3) mergear el borrador de la política de privacidad. Los modelos son de la serie 3: los 2.5 ya no están disponibles para cuentas nuevas (404) | 🤖 ✅ + 🧑 | — |
| 2.2 | ~~**Leer y clasificar recibos**~~ — **hecho**: `POST /ai/receipt/` devuelve una candidata editable (monto, fecha, comercio, ítems, confianza por campo), con la categoría resuelta primero por historial y después por IA, y los posibles duplicados. En la app, botón "Escanear recibo" en el alta de gasto | 🤖 ✅ | — |
| 2.3 | ~~**Entrada por texto libre (NLP)**~~ — **hecho**: `POST /ai/parse/` devuelve la misma candidata que los recibos, más el tipo y la cartera si la frase los nombra. Al modelo se le pasan los nombres reales de carteras y categorías para que elija de una lista cerrada. En la app, un campo de una línea en el alta | 🤖 ✅ | — |
| 2.4 | **Canal de Telegram** — bot, webhook, vinculación de cuenta con token de un uso. Ya entra por `/ai/parse/`: no lleva parser propio | 🤖 + 🧑 | 1 jornada |
| 2.5 | **Voz / dictado** — `expo-audio` + audio directo a Gemini, mismo parser que 2.3 | 🤖 | 1 jornada |
| 2.6 | **Analítica** — decidir primero entre sin-cookies, GA4+Clarity con banner, o métricas propias; implementar web | 🧑 luego 🤖 | 0.5 jornada |
| 2.7 | **DTE por correo (JSON)** — reusa `apps/email_import` entero; es el que da datos más ricos (ítems, IVA) | 🤖 | 1.5 jornadas |
| 2.8 | **QR de factura** — captura y validación contra el portal de Hacienda | 🤖 | 0.5 jornada |
| 2.9 | **Resumen y consejos mensuales** — encima de `behavior_insights()`, que ya existe | 🤖 | 0.5–1 jornada |
| 2.10 | **Chat sobre tus finanzas** — el de mayor superficie de riesgo (aislamiento por workspace), va al final | 🤖 | 2 jornadas |

**Subtotal: ~6.5–8.5 jornadas** (eran 11–13; 2.1, 2.2 y 2.3 ya están).

---

## Fase 3 — Nativo y tiendas

Sólo cuando la web esté andando y cobrando. Acá aparecen costos y esperas que no dependen de
nosotros.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 3.1 | `eas init` (`extra.eas.projectId`) + dev client + primer build | 🤖 + 🧑 | 1 jornada |
| 3.2 | Credenciales de push: FCM (Android) y APNs (**cuenta de Apple Developer, ~$99/año**) | 🧑 | 0.5 jornada |
| 3.3 | GA4 y Clarity nativos (`react-native-firebase`, `@microsoft/react-native-clarity`) | 🤖 | 0.5 jornada |
| 3.4 | Fichas de tienda: capturas, textos, formularios de privacidad, cuenta de prueba para el revisor | 🧑 | 1–2 jornadas |
| 3.5 | Revisión de Apple/Google | — | **1–2 semanas de espera** |

**Subtotal: ~3–4 jornadas de trabajo + la espera de revisión.**

---

## Sueltos y opcionales

- [ ] **Revisar mipisto.net** (competencia). Bloqueado: el dominio está bloqueado por el proxy de
      red de las sesiones de Claude Code en la web. Se resuelve pegando el contenido de la
      landing, o corriéndolo desde una sesión local. 30 min cuando se destrabe.
- [ ] Dos puntos abiertos y **no bloqueantes** de la auditoría de producto
      (`moneyapp/docs/audit-tasks.md`): auditar a mano sombras/radios de `WalletRow` y compañía, y
      iconografía propia de categoría en vez de emoji (cambio grande, se decidió no hacerlo).
- [ ] Atajo de Apple Shortcuts: armar el Atajo en un iPhone y pegar el link de iCloud en
      `EXPO_PUBLIC_SHORTCUT_URL` (no se puede generar desde el backend).
- [ ] `DASHBOARD_API_TOKEN` + `DASHBOARD_WORKSPACE_ID` si querés el endpoint de saldo para
      Villa Wxlter.

---

## Estimado final

Una **jornada** = una sesión de trabajo efectivo como las de esta semana (implementar + tests +
documentar), no un día de calendario.

| Fase | Trabajo | De eso, tuyo (🧑) |
|---|---|---|
| 0 — Producción sólida | ~1.5 jornadas | casi todo |
| 1 — Antes de cobrar | ~1.5–2 jornadas | la mitad |
| 2 — Funciones nuevas | ~6.5–8.5 jornadas | poco |
| 3 — Nativo y tiendas | ~3–4 jornadas | la mitad |
| **Total** | **~12.5–16.5 jornadas** | |

**Traducido a calendario:** a 2–3 jornadas por semana son **7 a 10 semanas** para todo. Pero el
recorte que importa es otro: **las Fases 0 y 1 son ~3–3.5 jornadas y son lo único que necesitás
para abrir y cobrar**. Eso es una semana de trabajo, no dos meses. Todo lo de la Fase 2 es
producto nuevo que puede salir después, con usuarios adentro y decidiendo el orden con lo que
ellos pidan.

**Camino crítico y esperas que no controlamos:** DNS de Cloudflare y de Mailgun (horas, a veces
un día), verificación del dominio de correo, la cuenta de Apple Developer (hasta 48 h) y la
revisión de las tiendas (1–2 semanas). Conviene arrancar esos trámites el primer día de cada
fase, no cuando ya esté todo lo demás listo.

**Lo único que yo movería de orden:** 2.1 (base de IA con su cuota) y 2.2 (recibos) pueden entrar
en paralelo con la Fase 1, porque no dependen de cobrar. Lo que **no** conviene mover es la cuota
de consumo: sin ella, una sola cuenta intensiva cuesta más que su suscripción.
