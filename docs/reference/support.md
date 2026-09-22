# Soporte

## Propósito

Canal para que un usuario reporte un bug, haga una consulta o mande una
sugerencia desde la app, sin salir de ella. No pretende ser una mesa de
ayuda completa: el seguimiento real (cambiar estado, responder) se hace
desde el admin de Django — no hay panel de soporte separado todavía.

## Modelos principales

- **`SupportTicket`**: el reporte en sí — tipo (`bug`/`query`/`suggestion`),
  asunto, mensaje original (inmutable) y estado
  (`open`/`in_progress`/`resolved`). Guarda además `app_version` y
  `platform`, metadata automática del cliente para diagnosticar un bug sin
  tener que preguntarle a la persona en qué versión le pasó — ninguno de los
  dos es obligatorio (un cliente viejo o web podría no mandarlos).
- **`SupportTicketMessage`**: un mensaje del hilo de un ticket, del usuario
  (al abrirlo o agregando contexto después) o de soporte (respondido desde
  el admin). `is_staff_reply` es una foto fija de si el autor era staff en
  ese momento, para que el hilo no cambie de apariencia si el rol de esa
  persona cambia después.

## Endpoints

- `support-tickets/` (`SupportTicketViewSet`, `WorkspaceScopedViewSet`,
  `/api/v1/support-tickets/`) — crear y listar tickets. Cada usuario ve sólo
  los suyos propios (no es un buzón compartido del workspace, a diferencia
  de la mayoría de los recursos de dominio). Filtrable por `?status=` y
  `?type=`.
  - `POST support-tickets/{id}/reply/` — agrega un mensaje del usuario al
    hilo (más contexto después de abrir el ticket). No reenvía nada a
    Discord; sólo la apertura del ticket se notifica ahí.

## Reglas de negocio y decisiones no obvias

- **Notificación a Discord al crear** (`services.notify_new_ticket`): cada
  ticket nuevo se postea a un webhook de Discord (`SUPPORT_WEBHOOK_URL`) para
  que alguien lo vea sin tener que entrar al admin a revisar. Si no hay
  webhook configurado, no hace nada; si el POST falla, se loguea como
  warning pero **nunca** revienta la creación del ticket — éste ya quedó
  guardado y visible en el admin de todos modos. El mensaje se trunca a 500
  caracteres (`MAX_MESSAGE_PREVIEW`) porque Discord rechaza/trunca mensajes
  largos; el texto completo sigue disponible en el admin.
- **Ámbito por usuario, no por workspace**: a diferencia del resto de las
  vistas de dominio, `SupportTicketViewSet.get_queryset` filtra por
  `created_by=request.user` además de por workspace — un ticket es privado
  de quien lo abrió, ni siquiera lo ven otros miembros del mismo workspace.
- **Sin panel de soporte propio**: cambiar `status` o responder como staff
  se hace directamente en el admin de Django (`SupportTicketMessage` con
  `is_staff_reply=True`); no hay endpoint de API para eso.
