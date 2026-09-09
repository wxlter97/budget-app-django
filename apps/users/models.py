import pyotp
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.crypto import get_random_string


class User(AbstractUser):
    """
    Modelo de usuario del proyecto.

    Se define desde el inicio (aunque hoy no añade campos) porque cambiar
    AUTH_USER_MODEL después de la primera migración es muy costoso. Cualquier
    campo de perfil futuro (avatar, moneda preferida, locale, workspace por
    defecto) vive aquí.
    """

    email = models.EmailField("correo electrónico", unique=True)
    # La llena "Continuar con Google" con el `picture` del token; vacía para
    # cuentas creadas con usuario/contraseña que nunca hicieron login social.
    profile_photo_url = models.URLField("foto de perfil", blank=True, default="")
    # True para las cuentas creadas con "Continuar con Google" y para las de
    # usuario/contraseña que después vincularon su cuenta de Google desde
    # Herramientas → Cuenta. Sin esto, `GoogleLoginView` no puede distinguir
    # "primera vez que entra con este correo" (crear cuenta) de "ya existe
    # una cuenta con ese correo hecha con contraseña" (no dejar entrar por
    # Google sin que el dueño la vincule a propósito primero).
    google_linked = models.BooleanField("cuenta de Google vinculada", default=False)

    def __str__(self):
        return self.get_username()


def _generate_secret() -> str:
    return pyotp.random_base32()


class TwoFactorAuth(models.Model):
    """
    2FA por TOTP (Google Authenticator/Authy/1Password...), una fila por
    usuario -- se reusa la misma fila en cada ciclo activar/desactivar (no
    se borra al desactivar, sólo se limpia).

    Flujo:
      1. `setup()` genera un secreto NUEVO y lo guarda (todavía `enabled=False`
         -- un secreto generado no confirmado no debe poder usarse para
         entrar). El cliente lo muestra como QR (`provisioning_uri`).
      2. `enable(code)` confirma que el usuario configuró bien su app
         verificando un código real contra ESE secreto; si coincide, prende
         `enabled` y genera los códigos de respaldo (se devuelven una sola
         vez, en texto plano, nunca más).
      3. Desde ahí, el login pide el código en un segundo paso (ver
         `apps.users.api`) -- `verify_code()` acepta tanto un TOTP válido
         como uno de los códigos de respaldo (de un solo uso).
    """

    BACKUP_CODES_COUNT = 8

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="two_factor_auth"
    )
    secret = models.CharField(max_length=64, default=_generate_secret)
    enabled = models.BooleanField(default=False)
    # Hashes (make_password), nunca el código en texto plano -- igual que la
    # contraseña del usuario. Se consumen de a uno (`verify_code`).
    backup_codes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"2FA de {self.user} ({'activo' if self.enabled else 'pendiente'})"

    def provisioning_uri(self) -> str:
        label = self.user.email or self.user.get_username()
        return pyotp.TOTP(self.secret).provisioning_uri(name=label, issuer_name="Budget")

    def verify_totp(self, code: str) -> bool:
        code = (code or "").strip().replace(" ", "")
        if not code:
            return False
        # `valid_window=1`: tolera un desfase de ±30s entre el reloj del
        # teléfono y el del servidor -- sin esto, un código que ya cambió
        # justo al tipearlo se rechaza de pedo.
        return pyotp.TOTP(self.secret).verify(code, valid_window=1)

    def generate_backup_codes(self) -> list[str]:
        """Genera `BACKUP_CODES_COUNT` códigos nuevos, guarda sus hashes
        (`self.backup_codes`, sin `save()` -- eso lo hace el caller junto
        con los demás cambios) y devuelve los códigos EN CLARO, para
        mostrarlos una sola vez."""
        plain = [get_random_string(10, allowed_chars="ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(self.BACKUP_CODES_COUNT)]
        self.backup_codes = [make_password(code) for code in plain]
        return plain

    def consume_backup_code(self, code: str) -> bool:
        code = (code or "").strip().upper().replace(" ", "")
        if not code:
            return False
        for hashed in self.backup_codes:
            if check_password(code, hashed):
                self.backup_codes = [h for h in self.backup_codes if h != hashed]
                self.save(update_fields=["backup_codes"])
                return True
        return False

    def verify_code(self, code: str) -> bool:
        """TOTP normal o, si no matchea, un código de respaldo (se consume)."""
        return self.verify_totp(code) or self.consume_backup_code(code)
