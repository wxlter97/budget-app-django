"""
Resolución de `ModuleFlag` -- ver el docstring del modelo en `models.py`.
Mismo criterio fail-open que `apps.billing.services`: una clave sin fila
todavía se considera habilitada.
"""
from __future__ import annotations

from rest_framework.exceptions import APIException

from .models import ModuleFlag

_DEFAULT_DISABLED_MESSAGE = "Esta función está desactivada por el momento. Volvé a intentarlo más tarde."


class ModuleDisabled(APIException):
    """503 y no 403: apagar un módulo es una decisión operativa (algo está
    fallando), no un límite de plan -- el cliente no debería ofrecer un
    upsell para esto."""

    status_code = 503
    default_code = "module_disabled"
    default_detail = _DEFAULT_DISABLED_MESSAGE


def module_enabled(key: str) -> bool:
    flag = ModuleFlag.objects.filter(key=key).first()
    return flag is None or flag.is_enabled


def require_module_enabled(key: str) -> None:
    flag = ModuleFlag.objects.filter(key=key).first()
    if flag is not None and not flag.is_enabled:
        raise ModuleDisabled(flag.disabled_message or _DEFAULT_DISABLED_MESSAGE)


def disabled_modules() -> dict[str, str]:
    """``{key: mensaje}`` de los módulos apagados ahora -- para el endpoint
    que el cliente consulta una vez y usa para esconder/avisar sin esperar a
    que el usuario toque el botón y se tope con el 503."""
    return {
        f.key: f.disabled_message or _DEFAULT_DISABLED_MESSAGE
        for f in ModuleFlag.objects.filter(is_enabled=False)
    }
