# Alta rápida (Quick Add)

## Propósito

Permite registrar un gasto con una sola llamada HTTP desde un cliente que no
puede (o no debe) manejar el login JWT normal de la app — hoy el único caso
real es un Atajo de Apple Shortcuts disparado por la notificación de Apple
Pay, pero el mecanismo sirve para cualquier integración externa futura. La
idea es que el body sólo necesite el monto y el comercio; todo lo demás
(usuario, workspace, cartera) queda fijo en la credencial.

## Modelos principales

- **`PersonalAccessToken`**: credencial de larga duración, atada de una vez a
  un usuario, un workspace y **una cartera fija**. El JWT normal dura minutos
  y se renueva solo; un Atajo no tiene forma de "iniciar sesión" cada vez, así
  que necesita algo que viva indefinidamente y se pueda revocar sin afectar la
  sesión de nadie más. Guarda sólo el hash del token (`token_hash`, SHA-256) y
  un `prefix` visible (`bt_live_9f2c4a…`) para que el dueño lo reconozca en la
  lista sin poder reconstruirlo — el valor crudo no se persiste en ningún
  lado y sólo viaja una vez, en la respuesta del `create()`.

## Endpoints

- `personal-tokens/` (`PersonalAccessTokenViewSet`, bajo el router,
  `/api/v1/personal-tokens/`) — crear, listar y borrar tokens propios dentro
  del workspace activo (JWT normal + `X-Workspace-ID`). Cada usuario sólo ve
  y borra los suyos. Crear exige la función Pro `quick_add`. Borrar es
  físico, no soft-delete: un token revocado no tiene papelera.
- `POST /api/v1/quick-add/` (`QuickAddView`) — el alta rápida en sí. Se
  autentica con `Authorization: Bearer bt_live_...`
  (`PersonalAccessTokenAuthentication`), no con JWT.

## Reglas de negocio y decisiones no obvias

- **Autenticación propia, no JWT**: `PersonalAccessTokenAuthentication`
  busca el token por `hash_token(raw)` y, si existe, deja como efecto
  lateral `request.quickadd_token` (además de `request.user`) — ahí ya
  vienen resueltos el workspace y la cartera fijos del token, así
  `QuickAddView` no necesita pedirle nada más al cliente que monto y
  comercio. Cada llamada actualiza `last_used_at` (para que el usuario vea
  si un token sigue en uso).
- **Un token = una cartera**: no es una limitación grave en la práctica —
  cada Atajo/dispositivo representa "una cartera" (p. ej. la tarjeta de
  Apple Pay); para otra cartera se genera otro token.
- **Categoría "auto"**: si `category` se omite o es `"auto"` (default), se
  adivina por la categoría más frecuente entre transacciones pasadas cuya
  descripción contenga el comercio (`guess_category_by_merchant`) — el mismo
  mecanismo que usa `email_import`. Si no hay suficiente historial para
  adivinar, la respuesta es 400 con la lista completa de categorías
  asignables del workspace, para que el Atajo muestre un menú nativo y
  reintente mandando el `id` elegido. `category` acepta también un id o un
  nombre exacto (case-insensitive) de categoría.
- **Duplicados**: antes de crear, se chequea `find_possible_duplicates`
  (misma cartera, monto, fecha cercana); si hay coincidencia, responde 409
  en vez de duplicar el gasto — cubre el caso de un Atajo que se dispara dos
  veces por la misma notificación.
- **Re-chequeo de la función Pro en cada request**: `require_feature_for_workspace(workspace, "quick_add")`
  se valida en el `POST` de alta, no sólo al emitir el token — si el
  workspace bajó a Free después (venció la suscripción), un token viejo deja
  de funcionar aunque siga existiendo.
- **`source=Transaction.SOURCE_QUICK_ADD`**: la transacción creada queda
  marcada con ese origen, distinguible de una carga manual o de
  `email_import` en reportes/auditoría.
