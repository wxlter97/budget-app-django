"""
Cuota mensual de IA por usuario, atada al plan.

Por qué existe antes que cualquier función de IA: una cuenta sin tope se come
el plan entero. Los números del backlog (`moneyapp/docs/backlog-nuevas-
funciones.md` → "Costos de IA y topes por plan") dan un usuario intensivo de
~12 ¢ al mes contra un Plus de $0.99 — pero **sin tope**, 500 recibos y 500
chats son ~$1.95, o sea el doble de lo que esa persona paga. El tope no es una
optimización, es lo que hace que el producto no pierda plata con su mejor
usuario.

Los límites viven en `Plan.features` (base de datos, `seed_billing_plans`), no
cableados acá: ajustarlos no debería necesitar un deploy.

**Una diferencia deliberada con el resto de `apps.billing`:** los chequeos de
features de allá son *fail-open* (sin plan configurado, la feature se
considera habilitada) porque el costo de equivocarse es que alguien vea una
pantalla de más. Acá el costo de equivocarse es una factura, así que esto es
*fail-closed*: un plan al que le falte la clave cae en `FALLBACK_LIMITS`, que
son los números del plan gratis.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.billing.services import plan_for_user

from . import models as m

# Operación → clave en `Plan.features`. Una operación que no esté acá no gasta
# cuota: es el caso del resumen mensual, que lo dispara el servidor y no el
# usuario, y que igual se registra en `AIUsage` para que se vea en el gasto.
PLAN_FEATURE_KEYS = {
    m.OP_RECEIPT: "ai_receipts_per_month",
    m.OP_PARSE: "ai_parses_per_month",
    m.OP_CHAT: "ai_chats_per_month",
}

# Lo que se aplica cuando el plan no dice nada (entorno sin seedear, plan
# viejo, clave nueva todavía no agregada). Son los números del plan gratis.
FALLBACK_LIMITS = {
    m.OP_RECEIPT: 3,
    m.OP_PARSE: 10,
    m.OP_CHAT: 0,
}

_UNLIMITED = None


class QuotaExceeded(APIException):
    """429 con código propio para que el cliente lo distinga del rate limit
    normal de DRF, que devuelve el mismo status: uno se resuelve esperando un
    minuto y el otro esperando al mes que viene o pasando de plan."""

    status_code = 429
    default_code = "ai_quota_exceeded"
    default_detail = "Se agotó tu cuota de IA de este mes."


def month_start(now=None) -> dt.datetime:
    """Primer instante del mes en curso, en la zona horaria del proyecto.

    La cuota se cuenta por mes calendario y no por ventana móvil de 30 días
    porque es lo que la gente entiende cuando lee "30 recibos al mes", y
    porque hace que la respuesta a "¿cuándo se me repone?" sea una fecha.
    """
    now = timezone.localtime(now or timezone.now())
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_reset(now=None) -> dt.datetime:
    start = month_start(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def limit_for(user, operation: str):
    """Tope mensual de esa operación para el plan del usuario.

    `None` = sin tope. Eso pasa cuando la operación no gasta cuota (resumen)
    o cuando el plan pone la clave explícitamente en `null`.
    """
    key = PLAN_FEATURE_KEYS.get(operation)
    if key is None:
        return _UNLIMITED

    plan = plan_for_user(user)
    features = getattr(plan, "features", None) or {}
    if key not in features:
        return FALLBACK_LIMITS[operation]

    value = features[key]
    if value is None:
        return _UNLIMITED
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        # Una clave mal escrita en el admin no debe convertirse en barra
        # libre: se cae al número del plan gratis y sigue.
        return FALLBACK_LIMITS[operation]


def used_this_month(user, operation: str, now=None) -> int:
    return m.AIUsage.objects.filter(
        user=user,
        operation=operation,
        counts_against_quota=True,
        created_at__gte=month_start(now),
    ).count()


def remaining(user, operation: str, now=None):
    """Cuántas quedan, o `None` si no hay tope."""
    limit = limit_for(user, operation)
    if limit is _UNLIMITED:
        return None
    return max(0, limit - used_this_month(user, operation, now=now))


def check(user, operation: str, now=None) -> None:
    """Levanta `QuotaExceeded` si ya no quedan. Se llama ANTES de gastar la
    llamada a Gemini, no después."""
    limit = limit_for(user, operation)
    if limit is _UNLIMITED:
        return
    used = used_this_month(user, operation, now=now)
    if used < limit:
        return

    label = dict(m.OP_CHOICES).get(operation, operation).lower()
    if limit == 0:
        detail = (
            f"Tu plan no incluye {label}. Pasate a Plus o Pro para usarlo."
        )
    else:
        cuando = timezone.localtime(next_reset(now)).strftime("%d/%m")
        detail = (
            f"Usaste las {limit} de este mes ({label}). Se repone el {cuando}, "
            "o podés pasar de plan para tener más."
        )
    raise QuotaExceeded(detail)


def status_for(user, now=None) -> dict:
    """Lo que el front necesita para decidir si muestra las entradas de IA y
    para poder avisar "te quedan N de N" antes de que el usuario choque."""
    quotas = {}
    for operation in PLAN_FEATURE_KEYS:
        limit = limit_for(user, operation)
        used = used_this_month(user, operation, now=now)
        quotas[operation] = {
            "limit": limit,
            "used": used,
            "remaining": None if limit is _UNLIMITED else max(0, limit - used),
        }
    return {"quotas": quotas, "resets_at": next_reset(now)}
