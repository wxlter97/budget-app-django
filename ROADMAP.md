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

- Los PRs #55 (backend) y #74 (front) **están mergeados a `main`**. El código de producto está
  al día: reembolsos, división entre personas, Personas, ahorro con interés, gamificación,
  insights de comportamiento, y los arreglos de escala del backend.
- La app corre en web (Vercel Hobby). **No hay build nativo publicado** y no hay
  `extra.eas.projectId`.
- 735 tests en el backend, 239 en el front, todos pasando.
- Lo que falta no es código de producto: es configuración, cobrar, y las funciones nuevas.

---

## Fase 0 — Producción sólida (antes de invitar a nadie)

Esto va primero porque construir funciones encima de una instalación que pierde los recibos y no
corre las tareas diarias es tirar trabajo. Casi todo es 🧑.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 0.1 | **`GS_BUCKET_NAME`** — sin esto los recibos se borran en cada deploy | 🧑 | 20 min |
| 0.2 | **Cloud Scheduler + Job `budget-cron`** — sin esto no corre nada diario (recordatorios, recurrentes, insights) y no hay error que avise | 🧑 | 30 min |
| 0.3 | **Endpoint *pooled* de Neon** en `DATABASE_URL` (`common.W002` lo avisa al arrancar) | 🧑 | 10 min |
| 0.4 | **`CACHE_URL`** con Redis de Upstash — el throttling ya no es correcto con 5 instancias (`common.W001`) | 🧑 | 30 min |
| 0.5 | **Mover el front a Cloudflare Pages** — Hobby no permite uso comercial. Confirmar primero dónde está la zona DNS (`DEPLOY.md` §3-C) | 🧑 | 1–2 h |
| 0.6 | **Job `budget-migrate` + `RUN_MIGRATIONS=0`** — saca la carrera de migraciones entre instancias y acelera el arranque en frío (`DEPLOY.md` §2.2) | 🧑 | 30 min |
| 0.7 | **Sentry** en los dos repos — hasta que esté, los errores de producción sólo se ven si alguien los cuenta | 🧑 | 30 min |
| 0.8 | **Backups**: `pg_dump` a GCS desde el job diario (Neon free retiene 24 h) | 🤖 + 🧑 | 1 h |
| 0.9 | **Ping de keepalive** del Scheduler, opcional pero se nota (`DEPLOY.md` §6.3) | 🧑 | 15 min |
| 0.10 | **Correo saliente (Mailgun)** — sin esto las invitaciones a workspace no llegan; incluye DNS y esperar propagación | 🧑 | 2–3 h |
| 0.11 | **Push web (VAPID)**: `manage.py generate_vapid_keys` | 🧑 | 15 min |
| 0.12 | **`SUPPORT_WEBHOOK_URL`** (Discord) para que los tickets avisen | 🧑 | 10 min |

**Subtotal: ~1.5 jornadas**, casi todas tuyas. 0.1 a 0.5 son el mínimo real para abrir.

---

## Fase 1 — Antes de cobrar

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 1.1 | **Wompi**: `WOMPI_API_KEY`, `WOMPI_WEBHOOK_SECRET`, y **verificar la tarifa real** (el fijo por cargo pesa ~30% en un plan de $0.99 — ver `COSTOS-Y-ESCALA.md`) | 🧑 | 1 h |
| 1.2 | `manage.py seed_billing_plans` + `grandfather_existing_users` si ya hay usuarios | 🧑 | 15 min |
| 1.3 | **Probar el flujo completo de punta a punta**: trial → cobro → webhook → activación → cancelación | 🤖 + 🧑 | 2–3 h |
| 1.4 | **Revisar precios a la luz del costo real.** Empujar el anual; decidir si el lifetime de $19.99 se mantiene (con IA es ~14 años de consumo para empatar) | 🧑 | decisión |
| 1.5 | **Legal**: revisar `privacy.tsx` y términos contra lo que de verdad va a hacer la app (IA, analítica, terceros). Hoy la política promete que no hay rastreadores de terceros | 🤖 + 🧑 | 2 h |
| 1.6 | **Catálogo de lealtad** (`Bank`, `CardProduct`, `LoyaltyProgram`, tasas) y mapear categorías a `CategoryType` — sin filas, puntos y cashback nunca se calculan | 🧑 | 2–3 h |
| 1.7 | **Schemas de correo bancario** (`BankEmailSchema`), uno por banco; sin ellos toda importación falla | 🤖 + 🧑 | 1–2 h por banco |

**Subtotal: ~1.5–2 jornadas** (más lo que sume cada banco).

---

## Fase 2 — Funciones nuevas (el trabajo grande)

Diseño completo, decisiones y costos: `moneyapp/docs/backlog-nuevas-funciones.md`.
Orden pensado para que cada pieza apoye la siguiente.

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| 2.1 | **Base de IA** (`apps/ai`): cliente de Gemini, `GEMINI_API_KEY`, throttle, **cuota mensual por plan** y log de consumo. Sin la cuota no se sigue | 🤖 | 1.5–2 jornadas |
| 2.2 | **Leer y clasificar recibos** — el que más se nota; reusa `Transaction.receipt` y `expo-image-picker`, que ya están | 🤖 | 1.5 jornadas |
| 2.3 | **Entrada por texto libre (NLP)** — reusa `apps/quickadd` y `guess_category_by_merchant` | 🤖 | 1 jornada |
| 2.4 | **Canal de Telegram** — bot, webhook, vinculación de cuenta con token de un uso | 🤖 + 🧑 | 1 jornada |
| 2.5 | **Voz / dictado** — `expo-audio` + audio directo a Gemini, mismo parser que 2.3 | 🤖 | 1 jornada |
| 2.6 | **Analítica** — decidir primero entre sin-cookies, GA4+Clarity con banner, o métricas propias; implementar web | 🧑 luego 🤖 | 0.5 jornada |
| 2.7 | **DTE por correo (JSON)** — reusa `apps/email_import` entero; es el que da datos más ricos (ítems, IVA) | 🤖 | 1.5 jornadas |
| 2.8 | **QR de factura** — captura y validación contra el portal de Hacienda | 🤖 | 0.5 jornada |
| 2.9 | **Resumen y consejos mensuales** — encima de `behavior_insights()`, que ya existe | 🤖 | 0.5–1 jornada |
| 2.10 | **Chat sobre tus finanzas** — el de mayor superficie de riesgo (aislamiento por workspace), va al final | 🤖 | 2 jornadas |

**Subtotal: ~11–13 jornadas.**

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
| 2 — Funciones nuevas | ~11–13 jornadas | poco |
| 3 — Nativo y tiendas | ~3–4 jornadas | la mitad |
| **Total** | **~17–21 jornadas** | |

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
