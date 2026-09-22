# Notifications

## Propósito

Todo el sistema de avisos al usuario: push nativo (Expo) y push web (VAPID),
preferencias por usuario, el centro de notificaciones dentro de la app, y los
recordatorios diarios/mensuales que corren como job programado. No genera
datos financieros propios -- reutiliza siempre el cálculo ya hecho en
`apps.reports.services` (qué viene vencido, presupuesto vs. gasto real,
patrones de comportamiento) y decide encima de eso a quién avisar, con qué
mensaje y con qué frecuencia.

## Modelos principales

- **`PushDevice`**: un dispositivo/navegador de un usuario. `token` es el
  Expo Push Token para nativo (ios/android), o la URL `endpoint` de la
  `PushSubscription` para web (donde además hace falta `p256dh`/`auth`, las
  claves para cifrar el payload vía Web Push/VAPID). `token` es único
  globalmente: re-registrar el mismo token (reinstalar la app, cambiar de
  cuenta en el mismo dispositivo) reasigna el dueño en vez de acumular filas
  muertas.
- **`NotificationPreference`**: una fila **por usuario**, no por workspace
  (si sos miembro de varios presupuestos, sos la misma persona decidiendo si
  el aviso le interesa). Toggles: `remind_recurring`, `remind_installments`,
  `warn_budget` (+ `budget_threshold_pct`), `remind_low_balance`,
  `warn_statement_due` (+ `statement_due_days_before`), `warn_insights` (un
  solo toggle para los seis patrones de comportamiento) y
  `warn_monthly_summary` (independiente de `warn_insights` a propósito, para
  poder apagar uno sin el otro).
- **`NotificationLog`**: registro interno de "esto ya se avisó", con
  constraint único por (user, kind, dedupe_key). No tiene API ni se muestra
  en la app -- sólo evita que el job diario repita el mismo aviso si corre
  más de una vez sobre la misma ocurrencia/período.
- **`Notification`**: el centro de notificaciones que sí ve el usuario en la
  app. Cubre los mismos `kind` que `NotificationLog` (una fila por cada push
  que de verdad se mandó) más otras cosas que antes no tenían aviso
  centralizado: invitaciones pendientes (`invitation`), correos bancarios por
  revisar (`email_import_pending`) y avisos de suscripción
  (`subscription_renewal_due`/`subscription_expired`, ver
  `apps.billing.services.send_renewal_reminders`). Tiene `status`
  (`unread`/`read`/`resolved` -- `resolved` es para cuando la acción se toma
  desde la propia pantalla del objeto, p. ej. aceptar la invitación, no desde
  el centro de notificaciones) y `related_object_id` (texto plano, no FK
  genérico) para poder resolverla desde afuera.

## Endpoints

Todos bajo `/api/v1/`. `push-devices/` y `notifications/` van por el router
(`config/api_router.py`); `notification-preferences/` está declarado aparte
en `config/urls.py`.

**PushDevice** (`PushDeviceViewSet`, sólo `create` del CRUD estándar):
- `POST /push-devices/`: registra (o re-registra, upsert por `token`) el
  dispositivo del usuario autenticado. Si `platform=web`, exige `p256dh` y
  `auth` (sin eso no se puede cifrar nada).
- `GET /push-devices/vapid-public-key/` (`AllowAny`): la clave pública VAPID,
  para que el navegador la use en `PushManager.subscribe({applicationServerKey})`.
  Pública por diseño -- viaja tal cual dentro de la propia suscripción.
- `POST /push-devices/test/`: manda un push de prueba a todos los
  dispositivos del usuario autenticado y devuelve el resultado por
  dispositivo (para diferenciar "no hay dispositivos", "el servidor no pudo
  mandarlo" y "salió bien" -- si aun con "salió bien" no se ve, el problema
  es del navegador/SO).
- `POST /push-devices/unregister/`: da de baja un token (p. ej. al cerrar
  sesión), por `token` en el body.

**NotificationPreference** (`NotificationPreferenceView`, `RetrieveUpdateAPIView`):
- `GET`/`PATCH /notification-preferences/`: preferencias del usuario
  autenticado, creadas con los defaults la primera vez que se piden.

**Notification** (`NotificationViewSet`, sólo lectura + marcar leída -- las
crea siempre el backend, nunca el cliente):
- `GET /notifications/`: historial del usuario autenticado.
- `GET /notifications/unread-count/`: cantidad sin leer.
- `POST /notifications/{id}/read/`: marca una como leída.
- `POST /notifications/mark-all-read/`: marca todas como leídas.

## Reglas de negocio y decisiones no obvias

### Qué recordatorios existen y cuándo corren

Todo entra por una sola tarea de Celery Beat, `tasks.send_daily_reminders`
(diaria), que llama en orden:

1. **`notify_due_items`**: recurrentes y cuotas que vencen **mañana**
   (`upcoming_scheduled(since=mañana, until=mañana)`, ver `reports.md`). El
   título distingue income/expense/transfer para un recurrente (un sueldo
   recurrente no puede avisar "Gasto recurrente mañana").
2. **`notify_budget_thresholds`**: categorías del período de presupuesto en
   curso que ya cruzaron `NotificationPreference.budget_threshold_pct`
   (default 90%) sobre `budgeted + provision` (no sólo `budgeted` -- incluye
   el sobrante acumulado, ver `apps.reports.services.budget_vs_actual`).
   Título "Presupuesto superado" si `pct >= 100`, si no "Presupuesto casi
   agotado".
3. **`notify_low_balance`**: carteras visibles con `Wallet.
   low_balance_threshold` fijado cuyo `current_balance` cayó por debajo. Se
   avisa como mucho una vez por mes por cartera mientras siga baja (dedupe
   por (wallet, año-mes)) -- si sube y vuelve a bajar en el mismo mes, no se
   reavisa hasta el mes siguiente.
4. **`notify_statement_due`**: tarjetas cuyo estado de cuenta (el pago de
   contado completo, no cuota por cuota -- eso ya lo cubre `notify_due_items`
   vía `remind_installments`) vence dentro de
   `NotificationPreference.statement_due_days_before` días (default 3).
5. **`notify_insights`**: patrones de comportamiento de gasto (ver abajo).
6. **`notify_monthly_summary`**: resumen mensual (ver abajo).

Todos pasan por `_notify`, que hace tres cosas juntas: registra en
`NotificationLog` (dedupe), crea la fila en `Notification` (centro de
notificaciones -- pasa **aunque no haya ningún dispositivo registrado**, así
igual queda visible dentro de la app) y manda el push sólo si hay
dispositivos. `_notify` también gatea la feature `"notifications"` del plan
del workspace -- pero **sólo ahí**, no en `notify_user` genérico (que usan
también invitaciones y avisos de suscripción, que siguen disponibles en el
plan gratis). Importante: sin la feature, `_notify` corta **antes** de tocar
`NotificationLog`, para no dejar un registro de "ya se mandó" fantasma si el
workspace pasa después a un plan que sí la tiene.

### Cómo se arma cada mensaje

Cada `notify_*` arma su propio `title`/`body` con datos del cálculo de
`apps.reports.services`, en español y con el nombre del workspace al final
(`"... — {workspace.name}"`), porque un mismo usuario puede tener el aviso
de varios presupuestos compartidos. El `dedupe_key` de cada uno identifica la
ocurrencia concreta que no hay que repetir: `"{source_id}:{fecha}"` para
recurrentes/cuotas, `"{categoria}:{period_start}"` para presupuesto,
`"{wallet}:{año-mes}"` para saldo bajo, `"{wallet}:{fecha_vencimiento}"` para
estado de cuenta.

### Insights (`notify_insights`) y resumen mensual (`notify_monthly_summary`)

Ambos consumen `apps.reports.services.behavior_insights` (ver `reports.md`
para el detalle de los seis detectores), pero son dos caminos independientes
a propósito, con su propio toggle (`warn_insights` / `warn_monthly_summary`):

- `notify_insights` corre **sólo los lunes** (`INSIGHTS_WEEKDAY = 0`), no
  todos los días -- `behavior_insights` cuesta ~12 queries por membresía, y
  como la mayoría de los detectores dedupean por semana, correrlo a diario
  significaba que 6 de cada 7 cálculos se descartaban igual por el
  `dedupe_key`. El costo de esto: si el scheduler no corre ese lunes, la
  semana entera se salta (aceptable para un aviso de patrón; si dejara de
  serlo, el reemplazo sería un marcador semanal por membresía en vez de un
  día fijo).
- `notify_monthly_summary` corre **sólo el día 1** (`MONTHLY_SUMMARY_DAY`),
  y calcula `behavior_insights` con `today - 1 día` como referencia (el
  último día del mes recién cerrado, no el que arranca). Redacta un solo
  mensaje que conecta todos los patrones detectados ese mes -- por
  `apps.ai.summary.generate` (Gemini) si está disponible, con un texto
  armado a mano concatenando `title: body` de cada insight como respaldo si
  la IA no responde (`AIUnavailable`). La detección sigue siendo 100%
  determinista en ambos casos; la IA sólo redacta.
- Ambos calculan preferencias y dispositivos **una sola vez por usuario**
  (no por membership) dentro del loop, porque una persona en varios
  workspaces comparte preferencia y dispositivos.

### Push nativo (Expo) vs. push web (VAPID)

`send_push` reparte cada `PushDevice` según su `platform`: nativo va por la
Expo Push API (`https://exp.host/--/api/v2/push/send`, POST plano con
`urllib`, sin SDK), web va por Web Push (RFC 8291) cifrado contra VAPID vía
`pywebpush` (import diferido: sólo se necesita si de verdad hay algo que
mandar por ese canal, para no forzar la dependencia en todo el proyecto). Son
protocolos completamente distintos -- Expo no sabe nada de suscripciones web,
y viceversa. Un fallo (token vencido, Expo caído, sin red) en un dispositivo
o canal **no tumba el resto de los avisos del día**: se loggea (`logger.
warning`) y se sigue con los demás.

Si `VAPID_PRIVATE_KEY`/`VAPID_PUBLIC_KEY` no están configuradas (`.env`), el
push web se omite silenciosamente con un warning en logs -- no revienta el
resto de la corrida (nativo incluido) en un deploy que todavía no configuró
VAPID. Un push web que responde `404`/`410` (la suscripción ya no existe: se
revocó el permiso, se limpiaron los datos del sitio, etc.) borra el
`PushDevice` en el momento -- no vale la pena reintentarlo nunca más.

`send_test_push` (detrás de `POST /push-devices/test/`) es la única llamada
que expone el resultado por dispositivo al cliente: los nativos sólo se
reportan como "enviado a Expo" (Expo responde de forma asíncrona, no hay
forma de saber más desde acá), los web devuelven el status real del servicio
de push -- útil para diagnosticar "no me llega nada" sin poder ver los logs
del servidor.

### Nota

No se encontró documentación ni configuración de un "resumen mensual" con
canal distinto al push/Notification normal (p. ej. email) -- `warn_monthly_summary`
usa el mismo `_notify`/`send_push` que todo lo demás, sólo con su propio
`kind` y cadencia. Si en algún momento se pensó en mandarlo también por
correo, no está implementado en este código.
