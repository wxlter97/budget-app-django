# Usuarios (apps.users)

## Propósito

Todo lo de identidad y acceso a la cuenta: registro, login con
usuario/contraseña, login con Google, verificación en dos pasos (2FA),
cambio/alta de contraseña, y borrado de cuenta a pedido del usuario. Es la
puerta de entrada al resto del API -- todo lo demás asume un `User`
autenticado con JWT.

## Modelos principales

- **`User`** (extiende `AbstractUser`, `email` único). Campos propios:
  `profile_photo_url` (la llena Google al hacer login social),
  `google_linked` (si esta cuenta puede entrar por "Continuar con Google"
  -- no es lo mismo que "se creó con Google", ver reglas de negocio),
  `onboarding_completed` (default `True` a nivel de columna para no mostrar
  el tour a cuentas que ya existían antes de este campo; las cuentas
  realmente nuevas lo arrancan en `False` a mano en `RegisterSerializer`
  y `GoogleLoginView`).
- **`TwoFactorAuth`** -- una fila por usuario (`OneToOne`), reusada en cada
  ciclo activar/desactivar. Guarda el `secret` TOTP, `enabled`,
  `backup_codes` (hasheados con `make_password`, nunca en claro) y
  `confirmed_at`. `verify_code()` acepta un TOTP válido (`valid_window=1`,
  tolera ±30s de desfase de reloj) o un código de respaldo, que se consume
  al usarse.

## Endpoints

Todos bajo `/api/v1/auth/`.

**Registro y login**
- `POST /auth/register/` -- alta con usuario/contraseña, devuelve el
  usuario + tokens JWT ya listos.
- `POST /auth/token/` -- login normal. Si el usuario tiene 2FA activo, NO
  devuelve tokens: responde `{"two_factor_required": true, "mfa_token": ...}`
  y el flujo sigue en `/auth/2fa/verify/`.
- `POST /auth/token/refresh/`, `POST /auth/token/verify/` -- ciclo JWT
  estándar de `simplejwt`, con throttling propio (`auth`).
- `POST /auth/google/` -- "Continuar con Google": verifica el `id_token`
  contra Google y devuelve tokens propios.
- `POST /auth/google/link/` -- vincula una cuenta ya autenticada
  (usuario/contraseña) a Google.

**2FA**
- `GET /auth/2fa/` -- si está activo (no expone secreto ni códigos).
- `POST /auth/2fa/setup/` -- genera un secreto TOTP nuevo (`enabled=False`
  todavía) y la URI `otpauth://` para el QR.
- `POST /auth/2fa/enable/` -- confirma con un código real, activa, devuelve
  los códigos de respaldo en claro (única vez que se ven).
- `POST /auth/2fa/disable/` -- requiere la contraseña, no un código.
- `POST /auth/2fa/backup-codes/` -- regenera el set de códigos de respaldo
  (invalida los viejos).
- `POST /auth/2fa/verify/` -- segundo paso del login: `mfa_token` del
  primer paso + código, a cambio de los tokens reales.

**Cuenta**
- `GET/PATCH /auth/me/` -- perfil del usuario autenticado.
- `POST /auth/me/delete/` -- borra la cuenta propia. Irreversible.
- `POST /auth/password/change/` -- para una cuenta que ya tiene contraseña
  utilizable (pide la actual).
- `POST /auth/password/set/` -- para una cuenta que sólo entraba por Google
  y todavía no tiene contraseña propia (no hay "actual" que confirmar).

## Reglas de negocio y decisiones no obvias

**2FA es un segundo paso con un token propio, no un flag en el JWT
normal.** `TwoFactorAwareTokenObtainPairSerializer` valida usuario/
contraseña igual que siempre y, si hay 2FA activo, en vez de tokens reales
emite un `MFAChallengeToken`: mismo mecanismo JWT pero con `token_type`
distinto (`mfa_challenge`), vida de 5 minutos, y que `JWTAuthentication`
rechaza de plano contra cualquier otro endpoint -- no sirve para nada más
que canjearse en `/auth/2fa/verify/`. Así una contraseña correcta nunca
emite tokens utilizables sin pasar por el segundo factor.

**Desactivar 2FA pide la contraseña, no un código.** Si alguien perdió el
teléfono con la app de autenticación, seguir exigiendo un TOTP lo dejaría
afuera de su propia cuenta -- la contraseña es la única prueba que le queda.
No hay recuperación por correo/soporte documentada en este código para el
caso de perder también la contraseña.

**`google_linked` es "puede entrar por Google", no "se creó con Google".**
Una cuenta usuario/contraseña que nunca tocó Google tiene `google_linked
=False`; si alguien intenta "Continuar con Google" con ese correo,
`GoogleLoginView` lo rechaza explícitamente (no entra sola) y pide iniciar
sesión con la contraseña y vincular a propósito desde `GoogleLinkView`. La
razón de seguridad es directa: si bastara con que el correo matcheara, una
cuenta de Google ajena con el mismo correo (no necesariamente controlada
por el dueño real, aunque Google verifique `email_verified`) podría entrar
a una cuenta que nunca pidió login social.

**Verificación del `id_token` de Google es contra el endpoint `tokeninfo`,
no con la librería `google-auth`.** Mismo nivel de validación de
firma/expiración/audiencia, pero sin sumar una dependencia sólo para eso
(`apps.users.services.verify_google_id_token`). Chequea además
`email_verified=true` y, si `GOOGLE_CLIENT_IDS` está configurado, que el
`aud` del token sea uno de los permitidos.

**Contraseña: "cambiar" y "poner" son endpoints distintos a propósito.**
`ChangePasswordView` exige la contraseña actual y sólo tiene sentido si
`has_usable_password()` es `True`. Una cuenta creada sólo por Google recibe
`set_unusable_password()` en el alta (`GoogleLoginView`), así que no tiene
una "actual" que confirmar -- para esa cuenta el camino es
`SetPasswordView`, que sólo pide la nueva. Cada vista rechaza
explícitamente el caso que no le corresponde con un mensaje que apunta al
endpoint correcto.

**Borrado de cuenta: no es cascada ciega, hay un caso bloqueado.**
`delete_own_account` (`apps/users/services.py`) distingue tres situaciones
para los workspaces del usuario:
- Dueño y único miembro → el workspace se borra entero junto con la cuenta
  (es su presupuesto personal, nadie más pierde nada).
- Dueño con otros miembros → **bloquea el borrado** (`AccountDeletionBlocked`,
  400) hasta que transfiera la propiedad o saque a los demás -- mismo
  espíritu que "no se puede expulsar al último owner". El usuario no puede
  borrar su cuenta llevándose por delante el presupuesto de otras personas.
- Simple miembro (no dueño) → la membresía se borra en cascada
  (`Membership.user` es `CASCADE`) y el workspace sigue intacto para el
  resto.

  Las `Subscription` del usuario se borran en cascada también, sin
  retención -- el propio código deja la nota de que esto es aceptable
  **mientras no haya facturación real con obligación legal de retención**,
  y que hay que revisarlo antes de cobrar suscripciones de verdad (ver
  checklist de producción, sección legal). El borrado de cuenta existe
  porque una app que deja crear cuenta desde adentro tiene que dejar
  borrarla desde adentro también (Apple App Store Review Guideline
  5.1.1(v)), y es irreversible: no hay soft-delete de usuario.

**Nota de investigación:** no se encontró límite de intentos específico
para `/auth/2fa/verify/` más allá del throttle general `auth` compartido
con login/registro -- no hay backoff propio ni bloqueo de cuenta tras N
códigos fallidos.
