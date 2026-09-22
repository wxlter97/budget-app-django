# Importación de correos bancarios

## Propósito

Convierte las notificaciones de compra que mandan los bancos por correo en
candidatas de `Transaction`, sin que el usuario tenga que anotar el gasto a
mano. El usuario reenvía (o configura el reenvío automático de) esos correos
a una dirección propia del workspace; un webhook los recibe, un parser
específico de cada banco extrae monto/fecha/comercio/últimos 4 dígitos, y el
resultado queda pendiente de confirmación manual antes de convertirse en un
gasto real.

## Modelos principales

- **`BankEmailSchema`**: catálogo global (no por workspace) de bancos
  soportados — nombre, `sender_pattern` (regex sobre el remitente) y versión
  de parser. Agregar un banco nuevo es una fila acá + su función en
  `bank_parsers/<banco>.py`, sin tocar el resto del pipeline.
- **`EmailImportLog`**: un correo procesado, exitoso o fallido, por
  workspace. Guarda lo extraído (monto, comercio, fecha), el estado
  (`pending` / `confirmed` / `rejected` / `failed` / `auto_handled`) y, si se
  confirmó, la `Transaction` resultante. Existe para que un correo que el
  parser no reconoce (banco sin schema, o el banco cambió su formato) quede
  visible para revisión en vez de perderse silenciosamente.

## Endpoints

Bajo `/api/v1/` (router) y uno suelto en `config/urls.py`.

- `bank-email-schemas/` (`BankEmailSchemaViewSet`) — catálogo de bancos
  soportados. Lectura: cualquier autenticado (sólo activos si no es staff);
  escritura: sólo staff.
- `email-import-logs/` (`EmailImportLogViewSet`), por workspace, sólo lectura
  de lista/detalle — no se crean por API, los genera el webhook. Filtrable
  por `?status=`.
  - `POST email-import-logs/{id}/confirm/` — materializa la `Transaction` a
    partir del log (con overrides opcionales de wallet/categoría/monto/fecha/
    descripción). Gatea por función Pro `import_email` y por el
    `ModuleFlag` `email_import`.
  - `POST email-import-logs/{id}/reject/` — descarta la candidata.
  - `POST email-import-logs/clear-failed/` — soft-delete masivo del historial
    `failed` del workspace (limpieza; no toca pending/confirmed/rejected).
- `POST /api/v1/email-import/inbound/` (`InboundEmailWebhookView`) — el
  webhook que recibe el correo ya normalizado. Sin autenticación de usuario
  (`AllowAny`); se valida con secreto/firma (ver abajo). Siempre responde 202
  con `{log_id, status}`, incluso si el parseo falló.

## Reglas de negocio y decisiones no obvias

**Flujo completo** (`services.ingest_inbound_email`):

1. El proveedor de correo entrante (Mailgun/Postmark/genérico) normaliza el
   mensaje a `(to, sender, subject, text)` y lo postea al webhook.
2. `resolve_workspace(to)` saca el workspace del token en
   `import+<token>@dominio` (`Workspace.inbound_token`). Usa `iexact`, no
   `=`, porque casi todos los proveedores bajan el destinatario a minúsculas
   antes de mandarlo, mientras el token guardado puede tener mayúsculas
   (viene de `secrets.token_urlsafe`) — una comparación exacta pierde el
   workspace y se ve como un 404 "misterioso".
3. Si el remitente es el de confirmación de reenvío de Gmail, se maneja
   aparte (ver más abajo) y termina ahí — nunca llega a intentar un schema de
   banco.
4. Si no, se busca un `BankEmailSchema` activo cuyo `sender_pattern` (regex,
   case-insensitive) matchee el remitente (`_match_schema`). Un patrón mal
   escrito (regex inválida) se degrada a comparación de substring literal en
   vez de reventar.
5. Se corre el parser registrado para ese banco (`bank_parsers.registry`,
   `slugify(schema.bank_name)` como key). Sin schema, sin parser, o si el
   parser levanta `ParseError` (formato no reconocido) → log `failed` con el
   motivo en `error_message`.
6. Si parsea bien, se intenta resolver la `Wallet`: primero por
   `card_last4` (matcheando también `Wallet.extra_cards`, para titulares +
   adicionales de la misma cuenta); si no hay match y el workspace tiene
   **exactamente una** cartera activa de ese banco, se asume esa — con dos o
   más queda ambigua y se deja sin asignar (el usuario la elige al
   confirmar).
7. Se crea el `EmailImportLog` en `pending` y se notifica a todos los
   miembros del workspace (no hay dueño único del log).
8. **Nunca se crea una `Transaction` en este paso** — sólo ocurre al llamar
   `confirm()`, que además exige la función Pro `import_email` y el
   `ModuleFlag` `email_import` activo (interruptor de emergencia aparte del
   plan: si el parseo de un banco empieza a confirmar montos/fechas mal, un
   admin apaga el módulo entero sin deploy). El log en sí se crea igual en
   cualquier plan, para que un workspace Free vea lo que se está perdiendo.

**Auto-confirmación del reenvío de Gmail** (`_confirm_gmail_forwarding_link`,
`bank_parsers`/`services.py`): Gmail exige "verificar" una dirección de
reenvío haciendo clic en un link de confirmación que manda un correo a esa
misma dirección — es decir, al propio webhook. Como nadie lee esa casilla,
sin un clic automático ningún usuario podría activar el reenvío automático
nativo de Gmail. El sistema detecta el remitente
`forwarding-noreply@google.com`, busca en el cuerpo un link a `google.com`
(o subdominio) y le hace un `GET` con `requests` (mismo efecto que un clic
humano), con timeout de 10s. Si encuentra y sigue el link, el log queda
`auto_handled`; si no hay link reconocible o el request falla, queda
`failed` para revisión manual. Esta es la razón de que `requests` esté en
`requirements.txt` (comentario explícito ahí: "auto-confirmar el link de
reenvío que manda Gmail").

**Autenticación del webhook** (`_authenticate_webhook`): dos modos, no
mutuamente excluyentes en el código pero sí en la práctica según el
proveedor — (1) Mailgun nativo: si `INBOUND_MAILGUN_SIGNING_KEY` está
configurada y el payload trae `timestamp`/`token`/`signature`, se verifica el
HMAC-SHA256 de Mailgun; (2) genérico: header `X-Inbound-Secret` comparado
(`hmac.compare_digest`) contra `INBOUND_WEBHOOK_SECRET`. Sirve para cualquier
proveedor que pueda mandar un header custom.

**Duplicados y categoría al confirmar**: `confirm()` reutiliza
`find_possible_duplicates` (misma cartera, monto y fecha cercana) — si
encuentra una coincidencia, responde 409 en vez de crear una segunda
transacción (correo reenviado dos veces, o el usuario ya lo había cargado a
mano). La categoría, si no se manda explícita, se adivina por comercio
(`guess_category_by_merchant`, aprendiendo de cómo se categorizó ese mismo
comercio antes); si no se puede adivinar, responde 400 con la lista de
categorías del workspace para que el cliente pregunte y reintente.

**Formatos de campo aceptados en el webhook**: además de `to`/`from`/
`subject`/`text`, se aceptan los nombres de Mailgun (`recipient`, `sender`,
`body-plain`, `stripped-text`) y Postmark (`To`, `From`, `TextBody`) — el
webhook no asume un único proveedor.

**Nota**: no queda claro en el código si además de correos reenviados a mano
el reenvío automático de Gmail es el único mecanismo soportado, o si también
se piensa integrar algún proveedor de "inbound parsing" distinto de
Mailgun/Postmark; el código está preparado para varios pero no hay
configuración de un tercero visible en `settings`.
