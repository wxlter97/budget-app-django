from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import PersonalAccessToken, hash_token

BEARER_PREFIX = "Bearer "


class PersonalAccessTokenAuthentication(BaseAuthentication):
    """
    ``Authorization: Bearer bt_live_...`` — sólo para :class:`QuickAddView`.

    A diferencia del JWT normal, deja como efecto lateral
    ``request.quickadd_token`` (la instancia) además de ``request.user``:
    ahí vienen ya resueltos el workspace y la cartera fijos de este token,
    así la vista no tiene que pedirle nada más al cliente.
    """

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith(BEARER_PREFIX):
            return None
        raw = header[len(BEARER_PREFIX):].strip()
        if not raw:
            return None

        try:
            token = PersonalAccessToken.objects.select_related(
                "user", "workspace", "wallet"
            ).get(token_hash=hash_token(raw))
        except PersonalAccessToken.DoesNotExist:
            raise AuthenticationFailed("Token inválido o revocado.")

        PersonalAccessToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        request.quickadd_token = token
        return (token.user, token)

    def authenticate_header(self, request):
        return "Bearer"
