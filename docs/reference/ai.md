# IA (apps.ai)

## Propósito

Integra Gemini (Google) para bajarle fricción a cargar transacciones y a
entender las finanzas del workspace: leer un recibo, entender una frase
suelta o un dictado de voz, responder preguntas sobre el presupuesto en
lenguaje natural, y redactar el resumen mensual. Sirve a cualquier usuario
autenticado con cuota disponible en su plan; el resto de la app (alta manual,
reportes) sigue funcionando exactamente igual si esta app está apagada o
Gemini está caído -- la IA nunca es una dependencia dura de una función que
ya existía sin ella.

## Modelos principales

- **`AIUsage`** -- una fila por cada llamada a Gemini (salga bien o mal). Es
  a la vez el contador de cuota (`used_this_month` hace un `COUNT` sobre esta
  tabla) y el detalle de gasto (`model`, `input_tokens`, `output_tokens`,
  `cost_micros`, `latency_ms`). No guarda nada de lo que el usuario escribió
  ni de lo que Gemini respondió -- sólo metadatos, para que un backup de esta
  tabla no filtre las finanzas de nadie. `operation` es uno de `receipt`,
  `parse`, `chat`, `summary`. `counts_against_quota` distingue si esa llamada
  le comió cupo al usuario (ver reglas de negocio).

No hay modelo de "candidata" persistida: el resultado de leer un recibo o
parsear una frase es un `dict` que la vista devuelve directo, nunca se
guarda en base hasta que el usuario confirma y crea la `Transaction` de
verdad (en `apps.transactions`).

## Endpoints

Todos bajo `/api/v1/ai/`, autenticados; los que operan sobre datos de un
workspace exigen además el header `X-Workspace-ID` (`HasWorkspaceMembership`)
y están limitados por el throttle scope `ai` (12/min).

- **`GET /ai/status/`** -- si esta instalación tiene IA (`GEMINI_API_KEY` +
  `ModuleFlag` "ai" prendido) y cuánta cuota le queda al usuario este mes por
  operación. Es el único endpoint de IA sin `X-Workspace-ID`: la cuota es por
  usuario, no por workspace. El front lo consulta una vez para decidir si
  muestra los botones de IA.
- **`POST /ai/receipt/`** -- sube una foto/PDF de un recibo (JPG, PNG, WEBP,
  HEIC o PDF, hasta 8 MB) y devuelve una candidata de transacción editable.
  No crea nada ni guarda el archivo.
- **`POST /ai/parse/`** -- convierte una frase en español ("gasté 12.50 en
  almuerzo con la tarjeta") en la misma clase de candidata, más `type` y
  `wallet`.
- **`POST /ai/voice/`** -- igual que `/ai/parse/` pero a partir de un audio
  (WAV, MP3, AAC, OGG, FLAC, hasta 15 MB); Gemini transcribe e interpreta en
  una sola llamada.
- **`POST /ai/chat/`** -- pregunta en lenguaje natural sobre las finanzas del
  workspace activo, responde texto libre más `function_used` (qué función de
  `apps.reports` se usó para contestar, o `null`).

El resumen mensual (`apps.ai.summary`) no tiene endpoint propio: lo dispara
el servidor (`apps.notifications.services._monthly_summary_text`), no el
usuario.

## Reglas de negocio y decisiones no obvias

**Todo pasa por `apps.ai.services.run()`.** Es el único camino que usan
recibos, parseo de texto/voz, chat y resumen: chequea el `ModuleFlag` "ai" y
la cuota, llama a Gemini, y registra el `AIUsage` -- éxito o error -- una
sola vez. Así el chequeo de cuota y el registro de consumo están escritos en
un solo lugar y no repetidos (con variantes sutiles) en cinco.

**Ninguna función de IA crea un registro directo.** Recibos, parseo de texto
y voz devuelven siempre una "candidata": campos editables con un nivel de
confianza (`high`/`medium`/`low`) por campo, nunca una `Transaction` ya
guardada. La razón es de producto: la IA acierta casi siempre, pero cuando
falla lo hace con la misma seguridad con la que acierta, y lo que está en
juego es el saldo de alguien. Por eso la regla dura es "vale más un campo
vacío que uno inventado" (`apps.ai.normalize`): un monto que no parsea, una
fecha futura o de hace más de 2 años, o un `wallet_hint`/`category_hint` que
no matchea exactamente un nombre de la lista que se le pasó al modelo, caen
a `None`/`low` en vez de a un valor adivinado.

**Categoría: primero el historial, después la IA.** Tanto en recibos como en
parseo de texto, `guess_category_by_merchant` (determinista, gratis, basada
en cómo *este* workspace categorizó *ese* comercio antes) se intenta primero;
sólo si no encuentra nada se usa la sugerencia del modelo. Nunca al revés.
`category_source` en la respuesta dice cuál de las dos fue.

**Cuota compartida por tipo de operación, no global.** `Plan.features` trae
tres claves independientes (`ai_receipts_per_month`, `ai_parses_per_month`,
`ai_chats_per_month`); texto y voz comparten el mismo contador
(`OP_PARSE`) porque para el usuario son la misma acción con dos entradas
distintas. Ausencia de la clave en el plan cae en `FALLBACK_LIMITS` (números
del plan gratis) -- **fail-closed**, a diferencia del resto de
`apps.billing`, que es fail-open ante un plan mal configurado. La razón:
acá equivocarse para el lado optimista es una factura de Google sin tope, no
sólo una pantalla de más. El resumen mensual (`OP_SUMMARY`) no está en
`PLAN_FEATURE_KEYS`: lo dispara el servidor, no consume cuota de nadie, pero
igual se registra en `AIUsage` para verse en el gasto.

**El chat hace dos llamadas reales a Gemini pero cuenta como una unidad de
cuota.** Primera llamada: el modelo elige (salida estructurada, de una lista
cerrada) qué función de `apps.reports.services` llamar y con qué
argumentos -- nunca ve una fila de la base ni genera SQL. Esa función se
ejecuta en código, con el `workspace`/`user` reales, y ya filtra por
carteras visibles. Segunda llamada: el modelo sólo redacta la respuesta a
partir de esos datos ya calculados. Las dos se registran juntas en un único
`AIUsage` (`chat._record`, sumando tokens/costo/latencia de ambas): se
factura lo que realmente cuesta, pero al usuario no le comen dos una
pregunta que hizo una vez. Si cualquiera de las dos falla, no se cobra nada
(`counts_against_quota=False`).

**Si Gemini falla o está caído, nunca es un 500.** `apps.ai.client` envuelve
cualquier fallo -- sin `GEMINI_API_KEY`, timeout, red caída, 4xx/5xx de
Google, JSON que no parsea, respuesta bloqueada por filtros de seguridad --
en `AIUnavailable`, con un `code` corto (sin datos del usuario, va a
`AIUsage.error_code`) y sin el cuerpo de la respuesta (que puede traer el
prompt de vuelta, por eso sólo va al log del servidor). Las vistas traducen
eso a **503**, distinto del 500 real, para que el cliente sepa "reintentá o
cargalo a mano" en vez de "algo se rompió". Se reintenta una sola vez, sólo
lo que tiene sentido reintentar (timeout, red, 429/5xx) -- nunca un 400 --
porque son llamadas interactivas con alguien mirando una pantalla de carga.
El resumen mensual, si Gemini no responde, cae al texto armado a mano con
los mismos datos deterministas de `apps.reports.services.behavior_insights`
-- el resumen nunca depende de que la IA esté arriba.

**Se usa la API REST de Gemini directo, no el SDK oficial.** Es un POST con
JSON y una respuesta con JSON; el SDK trae su propia cadena de dependencias
y ciclo de releases para lo mismo. Si algún día hacen falta streaming o
tools nativas del lado de Google, se reevalúa (`apps/ai/client.py`).

**Asignación de modelos y precios, todo en `pricing.py` y no desparramado.**
Flash-Lite para extracción de estructura a partir de texto corto (parseo,
chat: el modelo grande no acierta más ahí y cuesta ~6x); Flash para lo que
necesita visión (recibos) o razonar sobre el mes entero (resumen). Los
precios son una **estimación** para poder mirar el gasto sin esperar la
factura real de Google -- la factura real siempre manda. Nota del código:
los precios de `gemini-3.8-flash` son promocionales hasta el 31-dic-2026 y
suben después; y los modelos 2.5 ya no están disponibles para cuentas
nuevas (404) aunque sigan en la tabla, sólo para poder costear filas viejas.

**Carrera de cuota asumida, no resuelta con locking.** Dos requests en
paralelo del mismo usuario pueden pasar el chequeo de cuota a la vez y gastar
una llamada de más; el daño máximo es una unidad por ráfaga, acotado por el
throttle `ai` (12/min). Bloquear filas o reservar cupo se consideró
demasiada maquinaria para proteger centavos.

**Nota de investigación:** no se encontró documentación ni código sobre
límites de tamaño/duración de audio más allá de `AUDIO_MAX_SIZE` (15 MB) ni
sobre qué pasa si Gemini cambia el formato de `usageMetadata` -- no hay
manejo especial más que `_usage()` devolviendo 0 si la clave no está.
