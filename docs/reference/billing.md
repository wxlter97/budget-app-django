# Billing (apps.billing)

## Propósito

Maneja planes, precios, suscripciones y la integración con Wompi (pasarela
de pago salvadoreña, docs.wompi.sv) para cobrar el plan pago del producto.
Resuelve una pregunta central para el resto del backend -- "¿qué límites y
qué features tiene este usuario/workspace ahora mismo?" -- sin que ningún
otro app necesite saber nada de Wompi ni de cómo se factura. También cubre
los dos caminos de acceso gratis autoservicio (prueba gratis y códigos de
invitación) que existen aparte del cobro real.

## Modelos principales

- **`Plan`** -- vive en base de datos, no hardcodeado (gratis, Plus, Pro,
  cualquier cantidad). Límites numéricos (`max_workspaces_owned`,
  `max_members_per_workspace`, `max_active_recurring`, `None` = ilimitado),
  `trial_days`, y `features` (JSON libre: flags booleanos como
  `advanced_reports`/`export`/`loyalty`, y las tres cuotas mensuales de IA
  que consume `apps.ai.quotas`).
- **`PlanPrice`** -- un precio cobrable de un plan (mensual/anual/de por
  vida) en centavos. `external_refs` guarda el id que cada proveedor usa
  para identificar ese precio (`{"wompi": "..."}`); un proveedor sin entrada
  ahí no puede cobrarlo (`needs_external_ref`).
- **`Subscription`** -- una suscripción de un usuario a un `Plan`/`PlanPrice`
  con `status` (`pending`/`active`/`past_due`/`canceled`/`expired`),
  `provider` (`manual`/`wompi`), `is_trial`, `checkout_reference` (UUID
  generado ANTES de mandar al checkout, para poder correlacionar el primer
  webhook aunque todavía no se conozca el id del proveedor) y
  `current_period_end`. La propiedad `is_in_force` (activa o en gracia por
  pago vencido, y sin vencer) es lo único que el resto del código debería
  mirar para decidir "¿está pagando?".
- **`PromoCode` / `PromoCodeRedemption`** -- código de invitación canjeable
  una sola vez por usuario en toda su vida, que da acceso gratis a un plan
  sin pasar por ningún proveedor (`provider=manual`).
- **`ProcessedWebhookEvent`** -- registro de webhooks ya aplicados
  (`provider` + `event_id` único), para no procesar dos veces un reintento
  del proveedor.

## Endpoints

Bajo `/api/v1/billing/` salvo `PlanViewSet`, que va por el router en
`/api/v1/plans/`.

- **`GET/GET·id /plans/`** -- catálogo de planes con sus precios activos,
  para la pantalla de upgrade. Autenticado, sin filtrar por plan actual.
- **`GET /billing/me/`** -- el plan efectivo del usuario (el de su
  suscripción vigente, o el default/gratis) más el detalle de esa
  suscripción si tiene una.
- **`POST /billing/checkout/`** -- arranca un cobro con un `PlanPrice`:
  crea una `Subscription` en `pending` y devuelve la URL del proveedor a la
  que redirigir al usuario. `provider` es opcional (usa
  `DEFAULT_PAYMENT_PROVIDER` si no se manda).
- **`POST /billing/cancel/`** -- cancela la suscripción vigente del usuario
  vía el proveedor correspondiente.
- **`POST /billing/trial/`** -- arranca la prueba gratis de `plan.trial_days`
  sin pasar por ningún proveedor.
- **`POST /billing/redeem/`** -- canjea un código de invitación.
- **`POST /billing/webhooks/wompi/`** -- recibe los avisos de pago de Wompi
  (`AllowAny`, autenticado por firma HMAC, no por sesión).

## Reglas de negocio y decisiones no obvias

**Wompi está completamente implementado, no es un esqueleto.**
`WompiProvider` (`apps/billing/providers.py`) resuelve OAuth2 *client
credentials* con cacheo de token en memoria del proceso, arma el checkout
real -- un `EnlacePagoRecurrente` por compra para mensual (el día de cobro
es el día de la compra, tope 28; cancelar es desactivar ese enlace puntual)
y un `EnlacePago` único con `identificadorEnlaceComercio` = nuestra
`checkout_reference` para anual/de por vida (se renueva pagando otro
enlace) --, verifica la firma del webhook (`wompi_hash` = HMAC-SHA256 hex
del cuerpo crudo) y parsea el evento a un `WebhookEvent` normalizado. El
único comentario que sugiere lo contrario es un docstring viejo en
`PromoCode` ("ver `WompiProvider`, sin terminar") que quedó desactualizado y
debería corregirse -- el código real no tiene nada pendiente de esa
integración.

**Flujo completo trial → checkout → webhook → activación:**
1. **Trial** (`services.start_trial`): sin proveedor, sin código, elige un
   plan del catálogo directo. Un usuario sólo puede empezar UNA prueba en
   toda su vida (chequeo en código + constraint única en base
   `one_trial_subscription_per_user`, por si dos requests concurrentes -
   doble tap - pasan el chequeo a la vez).
2. **Checkout** (`CheckoutView` → `provider.create_checkout`): crea la
   `Subscription` en `pending` ANTES de llamar al proveedor -- así existe
   una fila con `checkout_reference` contra la cual el primer webhook puede
   matchear. Si el proveedor falla (`WompiError`) o no soporta ese flujo
   (`NotImplementedError`, caso `manual`), la `Subscription` se borra: no
   queda basura en `pending` por un checkout que nunca arrancó.
3. **Webhook** (`WompiWebhookView` → `apply_webhook_event`): busca la
   `Subscription` primero por `checkout_reference` (funciona incluso antes
   de conocer el id de Wompi, en el primer aviso) y si no, por
   `external_subscription_id` (renovaciones/cancelaciones posteriores). Si
   el monto pagado es menor al esperado del `PlanPrice`, NO activa -- evita
   que un enlace con monto editable o un aviso armado a mano regale el
   plan. Idempotente por `event.event_id` vía `ProcessedWebhookEvent` (un
   `IntegrityError` en el `create` significa "ya se aplicó, no hacer nada
   de nuevo").
4. **Activación**: `status` pasa a `active` y `current_period_end` se
   calcula sumando un período (1 o 12 meses) desde lo que ya tenía pagado
   si no había vencido (renovar antes no pierde días) o desde hoy si sí.

**Wompi sólo avisa cobros exitosos.** Un cobro recurrente fallido o un
anual que nadie volvió a pagar no manda ningún webhook -- la suscripción
simplemente deja de renovarse. Por eso existe
`services.send_renewal_reminders` (tarea diaria): es lo único que detecta
un vencimiento sin aviso del proveedor, comparando `current_period_end`
contra "ahora". También manda el recordatorio de "se renueva pronto" en la
ventana de 3 días antes. Es idempotente por `renewal_notice_sent_for`
comparado contra el `current_period_end` actual, así que una renovación
(que cambia esa fecha) vuelve a habilitar el aviso para el período nuevo.

**Cancelación es "no renovar", no "cortar ya".** `cancel_subscription`
desactiva el enlace recurrente en Wompi (si es mensual) pero el `status`
queda como estaba (`active`/`past_due`) -- el acceso sigue hasta
`current_period_end`, tal como promete la pantalla de "seguís teniendo
acceso hasta que termine el período ya pagado". La excepción es una
suscripción sin `current_period_end` (alta indefinida: grandfather, promo o
trial sin duración): ahí no hay período que esperar, corta al toque. El
código deja explícito un hallazgo propio: antes esa rama no existía y
siempre cortaba inmediato, contradiciendo el mismo mensaje que se le
mostraba al usuario -- ya está corregido.

**Downgrade no tiene endpoint propio.** No existe una ruta de "cambiar de
plan" -- el patrón es cancelar la actual (queda activa hasta que vence) y
dejar que expire, o empezar un checkout nuevo a otro `PlanPrice`. No se
encontró código que impida tener dos `Subscription` "vigentes" en paralelo
más allá de que `active_subscription_for` sólo devuelve la primera que
encuentra `is_in_force` (por `-created_at`) -- si se necesitara upgrade sin
esperar el vencimiento, es una función a construir, no algo que ya exista.

**Cobro por workspace vía su dueño, no por asiento.** `plan_for_workspace`
resuelve el plan del `Membership` con `role=owner`; los miembros invitados
heredan las features del plan del dueño sin pagar aparte.

**Fail-open en el resto de `apps.billing`, a diferencia de `apps.ai`.** Los
chequeos de límite (`can_own_another_workspace`, `can_add_member`,
`can_add_recurring`) y de features (`has_feature`) tratan "sin plan
configurado" (entorno sin `seed_billing_plans`) como "sin límite" -- el
costo de equivocarse ahí es que alguien vea una pantalla de más, no una
factura.

**El webhook depende de una configuración de infraestructura no obvia:**
`Wompi-Hash` es un header con guion bajo/mayúsculas mixtas y Gunicorn los
descarta salvo que se le pase `--header-map dangerous` (comentado en
`entrypoint.sh` según el propio código) -- sin eso, `verify_webhook` nunca
ve la firma y todo webhook real se rechaza como no verificado.

**Nota de investigación:** no se encontró en este código el manejo de
reembolsos ni de disputas/chargebacks de Wompi -- no hay `WebhookEvent.kind`
para eso. Tampoco hay lógica de prorrateo si alguien paga anual y quiere
bajar a mensual a mitad de período.
