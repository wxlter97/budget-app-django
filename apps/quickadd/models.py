"""
Credenciales de larga duración para clientes que no pueden (o no deben)
manejar el login JWT normal — hoy sólo un Atajo de Apple Shortcuts, pero
sirve para cualquier integración externa futura.

El JWT de la app dura minutos y se renueva solo; un Atajo no tiene forma de
"iniciar sesión" cada vez, necesita algo que viva indefinidamente y que se
pueda revocar sin tocar la sesión de nadie más. Por eso un
``PersonalAccessToken`` queda atado de una vez a un usuario, un workspace y
una cartera: el POST de "agregar un gasto" nunca necesita más que el monto y
el comercio.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models

from apps.accounts.models import Wallet
from apps.common.models import BaseModel
from apps.workspaces.models import Workspace

TOKEN_PREFIX = "bt_live_"
# Cuánto del valor crudo se guarda sin hashear, sólo para que el dueño
# reconozca el token en la lista ("bt_live_9f2c4a…") sin poder reconstruirlo.
PREFIX_VISIBLE_CHARS = len(TOKEN_PREFIX) + 6


def _generate_raw_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class PersonalAccessToken(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="personal_tokens"
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="personal_tokens"
    )
    # Cartera fija a la que se cargan los gastos de este token. No es una
    # limitación grave: cada Atajo/token representa "un dispositivo para una
    # cartera" (p. ej. la tarjeta de Apple Pay) — para otra cartera, otro token.
    wallet = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="personal_tokens"
    )
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=PREFIX_VISIBLE_CHARS, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def issue(cls, *, user, workspace, wallet, name):
        """Crea el token y devuelve ``(instancia, valor_crudo)``. El valor
        crudo no queda guardado en ningún lado — es la única vez que existe."""
        raw = _generate_raw_token()
        instance = cls.objects.create(
            user=user,
            workspace=workspace,
            wallet=wallet,
            name=name,
            token_hash=hash_token(raw),
            prefix=raw[:PREFIX_VISIBLE_CHARS],
        )
        return instance, raw
