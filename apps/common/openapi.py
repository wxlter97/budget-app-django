"""Hooks de postprocesado del esquema OpenAPI (drf-spectacular)."""

WORKSPACE_HEADER = "X-Workspace-ID"

# Endpoints bajo /api/v1/ que NO usan el header de workspace.
_EXCLUDED_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/workspaces/",
    "/api/v1/email-import/inbound/",
)

_METHODS = ("get", "post", "put", "patch", "delete")


def _needs_workspace_header(path: str) -> bool:
    return path.startswith("/api/v1/") and not path.startswith(_EXCLUDED_PREFIXES)


def add_workspace_id_header(result, generator, request, public):
    """
    Agrega el parámetro de header ``X-Workspace-ID`` (obligatorio) a todas las
    operaciones que lo requieren, en vez de repetir @extend_schema en cada view.
    """
    parameter = {
        "name": WORKSPACE_HEADER,
        "in": "header",
        "required": True,
        "description": (
            "UUID del workspace activo. Obligatorio en todos los endpoints "
            "salvo `auth/*` y `workspaces/*`."
        ),
        "schema": {"type": "string", "format": "uuid"},
    }

    for path, path_item in result.get("paths", {}).items():
        if not _needs_workspace_header(path):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _METHODS:
                continue
            params = operation.setdefault("parameters", [])
            if not any(p.get("name") == WORKSPACE_HEADER for p in params):
                params.append(parameter)

    return result
