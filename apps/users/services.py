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
