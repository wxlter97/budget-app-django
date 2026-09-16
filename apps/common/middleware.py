from django.conf import settings
from django.http import JsonResponse

# Rutas que siguen respondiendo aunque el mantenimiento esté activo: el
# healthcheck (para que Cloud Run no mate el servicio por creer que se cayó)
# y /admin/ (para poder seguir operando -- p. ej. desactivar el mantenimiento
# desde el shell, o el admin, mientras el resto del API está cerrado).
MAINTENANCE_EXEMPT_PREFIXES = ("/healthz", "/admin/")


class MaintenanceModeMiddleware:
    """
    Interruptor de emergencia manual: con `MAINTENANCE_MODE=True` (env var),
    todo el API responde 503 con un mensaje fijo en vez de tocar la base de
    datos -- para una ventana de mantenimiento (p. ej. una migración
    riesgosa) sin tener que apagar el servicio entero (ver RUNBOOK.md).

    Se activa/desactiva redeployando con la env var cambiada (Cloud Run) --
    no hay UI ni endpoint para esto a propósito: es un mecanismo de
    emergencia, no una feature de producto.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.MAINTENANCE_MODE and not request.path.startswith(MAINTENANCE_EXEMPT_PREFIXES):
            return JsonResponse(
                {"error": "maintenance", "detail": settings.MAINTENANCE_MESSAGE}, status=503
            )
        return self.get_response(request)
