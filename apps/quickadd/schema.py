"""Documentación OpenAPI para la autenticación por PersonalAccessToken.
Se registra vía `QuickaddConfig.ready()` — sin esto, drf-spectacular no sabe
cómo describir el esquema de seguridad de `QuickAddView` (avisa con un
warning en vez de fallar, pero el swagger queda incompleto)."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class PersonalAccessTokenScheme(OpenApiAuthenticationExtension):
    target_class = "apps.quickadd.authentication.PersonalAccessTokenAuthentication"
    name = "PersonalAccessTokenAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "bt_live_...",
            "description": "Token personal generado desde Herramientas → Atajos en la app.",
        }
