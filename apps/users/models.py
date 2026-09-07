from django.contrib.auth.models import AbstractUser
from django.db import models


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
