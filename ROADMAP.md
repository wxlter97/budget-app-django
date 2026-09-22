# Roadmap — de acá a producción, y después

> Escrito el 18 sep 2026, actualizado el 22 sep 2026. Es el índice maestro: junta lo que falta para salir a producción de
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

- El código de producto está al día: reembolsos, división entre personas, Personas, ahorro con
  interés, gamificación, insights de comportamiento, plan gratis restrictivo, recompensas de
  lealtad, y los arreglos de escala de backend y frontend.
- La Fase 0 está prácticamente cerrada: backup diario a GCS, Cloud Scheduler/job diario, Sentry,
  push web, correo saliente, alertas y Cloudflare Pages como hosting del front (ver la tabla de
  Fase 0 arriba). La Fase 1 avanzó fuerte: `WompiProvider` está implementado y probado (relay por
  Cloudflare Worker, cobro recurrente, avisos de vencimiento, tarifa real confirmada); 1.1, 1.4 y
  1.5 están cerrados. Lo único que falta para cobrar de verdad es configuración (credenciales
  reales de Wompi) y probar de punta a punta (1.3), no código.
- **De la Fase 2, ya están la base de IA con su cuota por plan (2.1), el escaneo de recibos
  (2.2), la entrada por texto libre (2.3), la voz/dictado (2.5), la analítica sin cookies (2.6),
  el resumen mensual (2.9) y el chat de finanzas (2.10)** — código y tests, pendiente sólo de
  crédito de prepago en AI Studio y de que se prenda el interruptor `ai` del admin (y, para 2.6,
  de crear la cuenta de Umami). Telegram (2.4), el DTE por correo (2.7) y el QR de factura (2.8)
  quedaron **diferidos por decisión** (22-sep-2026), no por bloqueo técnico.
- La app corre en web (Cloudflare Pages). **No hay build nativo publicado** y no hay
  `extra.eas.projectId`.
- 1134 tests en el backend, 388 en el front, todos pasando.
- Lo que falta para producto no es código: es configuración (Wompi, GCS, Umami) y las tres
  funciones de la Fase 2 que quedaron diferidas por decisión.

---

## Fase 0 — Producción sólida (antes de invitar a nadie)

Esto va primero porque construir funciones encima de una instalación que pierde los recibos y no
corre las tareas diarias es tirar trabajo. Casi todo es 🧑.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 0.1 | ~~**`GS_BUCKET_NAME`**~~ — **hecho**, el roadmap tenía texto viejo: verificado el 19-sep-2026 (`CONFIG-PENDIENTE.md`), el job `budget-cron` ya lo tiene y el backup sube a `backups/db/` | 🧑 ✅ | — |
| 0.2 | ~~**Cloud Scheduler + Job `budget-cron`**~~ — confirmado hecho por vos (22-sep-2026); no verificable desde este sandbox | 🧑 ✅ | — |
| 0.3 | ~~**Endpoint *pooled* de Neon** en `DATABASE_URL`~~ — **hecho** (verificado el 20-sep-2026): el host es `-pooler` y el servicio y el job tienen `DJANGO_DB_DISABLE_SERVER_SIDE_CURSORS=True` | 🧑 ✅ | — |
| 0.4 | **`CACHE_URL`** con Redis de Upstash — **diferido por decisión** (20-sep-2026): el throttling en memoria cuenta bien con una sola instancia (`maxScale: 1`, verificado el 19-sep) y sólo se reinicia en cada deploy o arranque en frío. Reabrir el día que se suba el máximo de instancias. Ojo: con Redis la API pasa a depender de un servicio externo, porque el backend de Redis de Django no falla en abierto | 🧑 | 30 min |
| 0.5 | ~~**Mover el front a Cloudflare Pages**~~ — **hecho** (20-sep-2026): proyecto de Pages `moneyapp-8jz`; `money.wxlter.dev` apunta por CNAME y la zona DNS no se movió del registrar. El despliegue de Vercel quedó pausado y su integración Git ya está desconectada (`DEPLOY.md` §3) | 🧑 ✅ | — |
| 0.6 | ~~**Job `budget-migrate` + `RUN_MIGRATIONS=0`**~~ — **hecho** (20-sep-2026): el job existe, el servicio tiene `RUN_MIGRATIONS=0` y `deploy.yml` migra antes de mover el tráfico (`DEPLOY.md` §2.2). Regla que trae: nunca borrar ni renombrar una columna en el mismo release que deja de usarla | 🧑 ✅ | — |
| 0.7 | ~~**Sentry** en los dos repos~~ — confirmado hecho por vos (22-sep-2026); no verificable desde este sandbox | 🧑 ✅ | — |
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
| 1.1 | ~~**Wompi**: implementar `WompiProvider`~~ — **corregido (22-sep-2026): ya está hecho**, el roadmap tenía texto viejo. `apps/billing/providers.py` implementa los 4 métodos (`create_checkout`, `verify_webhook`, `parse_webhook_event`, `cancel_subscription`) contra la API real: OAuth 2.0 *client credentials*, Enlace de Pago para anual/lifetime y **un `EnlacePagoRecurrente` por compra** (no compartido por plan) para mensual — así el `idEnlace` de cada persona queda en `Subscription.external_subscription_id` y cancelar es desactivar ESE enlace, no el de todos; webhook verificado con `wompi_hash` (HMAC-SHA256) y sólo avisa cobros exitosos. 49 tests en `apps/billing/tests/test_wompi.py` + `test_wompi_probe.py`, todos pasando. **Tarifa confirmada (22-sep-2026):** 3.5% de comisión de Wompi + 2% de anticipo de IVA, **sin cuota fija por cobro** (ejemplo dado: ~$0.10 sobre un cobro de $1.99) — detalle en `ECONOMIA-POR-PLAN.md`. Lo que falta es **configuración, no código**: `WOMPI_CLIENT_ID`/`WOMPI_CLIENT_SECRET` reales (ver `CONFIG-PENDIENTE.md`) | 🤖 ✅ + 🧑 | 1 h |
| 1.2 | `manage.py seed_billing_plans` + `grandfather_existing_users` si ya hay usuarios | 🧑 | 15 min |
| 1.3 | **Probar el flujo completo de punta a punta**: trial → cobro → webhook → activación → cancelación, contra el sandbox real de Wompi (`manage.py wompi_probe` ya existe para diagnosticar). **Ya no bloqueado por código** (ver 1.1) — bloqueado sólo por tener `WOMPI_CLIENT_ID`/`WOMPI_CLIENT_SECRET` de un negocio en modo desarrollo | 🧑, con 🤖 si algo falla | 1–2 h |
| 1.4 | ~~**Revisar precios a la luz del costo real.**~~ — **cerrado (22-sep-2026)** con la tarifa real de Wompi (ver 1.1): sin cuota fija, la comisión deja de castigar a Plus y Pro mensual (antes ~34%/~18%, ahora ~5.5% del precio). El lifetime de $19.99 sigue siendo la exposición real (1.6–2.4 años al techo de Pro) y sigue sin decidirse; el resto del análisis, actualizado en `ECONOMIA-POR-PLAN.md` | 🧑 ✅ | — |
| 1.5 | ~~**Legal**: revisar `privacy.tsx` y términos contra lo que de verdad va a hacer la app~~ — **confirmado listo por vos (22-sep-2026)**; el borrador ya mergeado a `main` es `c257e69` (sección "Inteligencia artificial") | 🤖 ✅ | — |
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
| 2.4 | **Canal de Telegram** — bot, webhook, vinculación de cuenta con token de un uso. Ya entra por `/ai/parse/`: no lleva parser propio. **Diferido por decisión (22-sep-2026):** no es prioridad ahora | 🤖 + 🧑 | 1 jornada |
| 2.5 | ~~**Voz / dictado**~~ — **hecho (22-sep-2026)**: `apps/ai/parsing.py` se separó en `_build_context`/`_candidate_from_response` compartidos y `parse_audio()`, que manda el audio como `inline_data` con `has_audio=True` (mismo precio de audio que ya tenía `pricing.py`). `POST /ai/voice/` comparte la cuota de `parse`, no es una operación aparte. En la app, `VoiceInputButton` con `expo-audio`: graba a `.m4a`/AAC en nativo, y en web sólo aparece si el navegador sabe grabar `audio/mp4` (Safari sí, Chrome/Firefox de escritorio no — ahí no se muestra el botón, en vez de grabar en un formato que el backend rechaza) | 🤖 ✅ | — |
| 2.6 | ~~**Analítica**~~ — **hecha (22-sep-2026)**: se decidió sin cookies (Umami Cloud). `src/lib/analytics.ts` (envoltorio `track()`) + script inyectado en `scripts/pwa-postbuild.js` (no en `app/+html.tsx`, que `web.output: "single"` ignora — confirmado con un build real). Eventos: alta de transacción por canal, presupuesto creado, invitación aceptada, inicio de trial, y el par inicio/fin de onboarding como proxy de abandono. La política de privacidad sigue siendo cierta tal cual está (Umami no usa cookies ni identifica personas): sin banner de consentimiento | 🧑 ✅ + 🤖 ✅ | — |
| 2.7 | **DTE por correo (JSON)** — reusa `apps/email_import` entero; es el que da datos más ricos (ítems, IVA). **Diferido por decisión (22-sep-2026):** no es prioridad ahora | 🤖 | 1.5 jornadas |
| 2.8 | **QR de factura** — captura y validación contra el portal de Hacienda. **Diferido por decisión (22-sep-2026):** no es prioridad ahora | 🤖 | 0.5 jornada |
| 2.9 | ~~**Resumen y consejos mensuales**~~ — **hecho (22-sep-2026)**: `apps/ai/summary.py` conecta los patrones de `behavior_insights()` (que sigue siendo 100% determinista) en un solo texto por Gemini; si la IA no responde, `notifications.services._monthly_summary_text` cae al texto armado a mano con los mismos datos. Corre una vez al mes (día 1, para el mes que terminó), kind propio (`monthly_summary`) y toggle propio (`warn_monthly_summary`) en Ajustes → Notificaciones, independiente del de "Patrones de gasto". No consume cuota (lo dispara el servidor, no el usuario) | 🤖 ✅ | — |
| 2.10 | ~~**Chat sobre tus finanzas**~~ — **hecho (22-sep-2026)**: `POST /ai/chat/` (`apps/ai/chat.py`) nunca toca la base ni genera SQL — dos llamadas a Gemini (elegir una función cerrada de `apps.reports.services` o ninguna, y sólo redactar con lo que esa función devuelve), contadas como **una sola** unidad de la cuota de chat aunque sean dos llamadas reales. Pantalla nueva "Chat de finanzas" en Herramientas → Análisis, historial sólo en memoria de la pantalla (no se guarda en el servidor, backlog punto 5) | 🤖 ✅ | — |

**Subtotal: ~2 jornadas** (eran 6.5–8.5; 2.1, 2.2, 2.3, 2.5, 2.6, 2.9 y 2.10 ya están — quedan 2.4, 2.7 y 2.8, las tres diferidas por decisión).

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
| 0 — Producción sólida | ~1.5 jornadas (prácticamente cerrada) | casi todo |
| 1 — Antes de cobrar | ~1.5–2 jornadas (1.1 tarifa confirmada, 1.4 y 1.5 cerrados) | la mitad |
| 2 — Funciones nuevas | ~2 jornadas (2.1, 2.2, 2.3, 2.5, 2.6, 2.9 y 2.10 ya están) | poco |
| 3 — Nativo y tiendas | ~3–4 jornadas | la mitad |
| **Total** | **~8–9.5 jornadas** | |

**Traducido a calendario:** a 2–3 jornadas por semana son **3 a 5 semanas** para todo lo que
queda (bajó de 7–10 al cerrarse gran parte de la Fase 2). El recorte que sigue importando: **las
Fases 0 y 1 son ~3–3.5 jornadas y son lo único que necesitás para abrir y cobrar**, y de eso ya
quedan pocos puntos sueltos (0.1, 0.4, 1.1–1.3, 1.6, 1.7). Lo que queda de la Fase 2 (2.4, 2.6,
2.7, 2.8) es producto nuevo que puede salir después, con usuarios adentro decidiendo el orden.

**Camino crítico y esperas que no controlamos:** DNS de Cloudflare y de Mailgun (horas, a veces
un día), verificación del dominio de correo, la cuenta de Apple Developer (hasta 48 h) y la
revisión de las tiendas (1–2 semanas). Conviene arrancar esos trámites el primer día de cada
fase, no cuando ya esté todo lo demás listo.

**Lo único que yo movería de orden:** 2.1 (base de IA con su cuota) y 2.2 (recibos) pueden entrar
en paralelo con la Fase 1, porque no dependen de cobrar. Lo que **no** conviene mover es la cuota
de consumo: sin ella, una sola cuenta intensiva cuesta más que su suscripción.
