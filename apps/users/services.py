"""Verificación de id_token de "Continuar con Google".

Usa el endpoint `tokeninfo` de Google en vez de la librería `google-auth`
(que verifica la firma localmente contra las claves públicas de Google): es
la misma validación de firma/expiración/audiencia pero sin sumar una
dependencia nueva solo para esto.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleTokenError(Exception):
    """El id_token de Google no es válido, expiró, o no es para esta app."""


def verify_google_id_token(id_token: str, *, timeout=5) -> dict:
    """Devuelve los claims del id_token (`email`, `email_verified`,
    `given_name`, `family_name`, `picture`, `aud`, ...) o lanza
    ``GoogleTokenError`` si no es válido."""
    if not id_token:
        raise GoogleTokenError("Falta el id_token.")

    url = f"{GOOGLE_TOKENINFO_URL}?id_token={urllib.parse.quote(id_token)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            claims = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GoogleTokenError("Token de Google inválido o expirado.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GoogleTokenError("No se pudo validar el token con Google.") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleTokenError("Respuesta inesperada de Google.") from exc

    allowed_client_ids = settings.GOOGLE_CLIENT_IDS
    if allowed_client_ids and claims.get("aud") not in allowed_client_ids:
        raise GoogleTokenError("El token no corresponde a esta app.")
    if str(claims.get("email_verified")).lower() != "true":
        raise GoogleTokenError("El correo de la cuenta de Google no está verificado.")
    if not claims.get("email"):
        raise GoogleTokenError("El token no trae un correo.")
    return claims


class AccountDeletionBlocked(Exception):
    """El usuario no puede borrar su cuenta todavía -- hay que resolver algo
    primero (mensaje en `args[0]`, pensado para mostrarse tal cual)."""


def delete_own_account(user) -> None:
    """
    Borra la cuenta del usuario autenticado, a pedido de la propia persona
    (una app que deja crear cuenta desde adentro tiene que dejar borrarla
    desde adentro también -- Apple App Store Review Guideline 5.1.1(v)).

    - Workspaces donde es el ÚNICO miembro: se borran enteros con ella (son
      su presupuesto personal, nadie más pierde nada).
    - Workspaces donde es OWNER pero hay otros miembros: bloquea el borrado
      con `AccountDeletionBlocked` -- primero hay que transferir la
      propiedad o sacar a los demás (mismo espíritu que "no podés expulsar
      al último owner", ver `apps.workspaces.api.MembershipViewSet`).
    - Workspaces donde es simple miembro: se van solas al borrar el usuario
      (`Membership.user` es `CASCADE`), el workspace sigue para el resto.

    Las `Subscription` del usuario se borran en cascada también -- aceptable
    mientras no haya facturación real con obligación de retención; revisar
    esto antes de cobrar suscripciones de verdad (ver checklist de
    producción, sección legal).
    """
    from django.db.models.deletion import ProtectedError

    from apps.workspaces.models import Membership, Workspace

    owned_ids = list(
        Membership.objects.filter(user=user, role=Membership.ROLE_OWNER, is_deleted=False)
        .values_list("workspace_id", flat=True)
    )
    blocking = (
        Membership.objects.filter(workspace_id__in=owned_ids, is_deleted=False)
        .exclude(user=user)
        .values_list("workspace__name", flat=True)
        .distinct()
    )
    if blocking:
        names = ", ".join(blocking)
        raise AccountDeletionBlocked(
            f"Tenés presupuestos compartidos con otras personas ({names}) -- "
            "transferí la propiedad o sacá a los demás miembros antes de borrar tu cuenta."
        )

    try:
        Workspace.objects.filter(id__in=owned_ids).delete()
        user.delete()
    except ProtectedError as exc:
        raise AccountDeletionBlocked(
            "No se pudo borrar tu cuenta por una referencia protegida -- contactá soporte."
        ) from exc
