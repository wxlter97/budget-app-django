"""
Capa de abstracción de proveedor de pago.

El resto de `apps.billing` (vistas, servicios) no debe importar nada
específico de Wompi -- solo esta interfaz. Sumar un proveedor nuevo (o
reemplazar Wompi el día de mañana) es agregar una clase acá y registrarla
en `_PROVIDERS`, sin tocar modelos ni vistas.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

from django.conf import settings


@dataclasses.dataclass
class CheckoutSession:
    checkout_url: str
    external_customer_id: str = ""


@dataclasses.dataclass
class WebhookEvent:
    """Evento ya normalizado -- el código que lo consume (`services.apply_webhook_event`)
    no sabe ni le importa de qué proveedor vino."""

    kind: str  # "subscription.activated" | "subscription.renewed" | "subscription.failed" | "subscription.canceled"
    reference: str = ""  # nuestro `Subscription.checkout_reference`, si el proveedor lo hizo ida y vuelta
    external_subscription_id: str = ""
    external_customer_id: str = ""
    current_period_end: datetime | None = None
    raw: dict = dataclasses.field(default_factory=dict)


class PaymentProvider:
    """Interfaz que implementa cada proveedor concreto."""

    code: str

    def create_checkout(self, *, user, plan_price, subscription, success_url: str, cancel_url: str) -> CheckoutSession:
        raise NotImplementedError

    def verify_webhook(self, request) -> bool:
        raise NotImplementedError

    def parse_webhook_event(self, request) -> WebhookEvent:
        raise NotImplementedError

    def cancel_subscription(self, subscription) -> None:
        raise NotImplementedError


class ManualProvider(PaymentProvider):
    """
    Sin proveedor externo: el alta/baja la hace un admin a mano desde
    ``/admin/`` (comps, pruebas, gestos de soporte -- crear una
    ``Subscription`` con ``provider=manual``, ``status=active``). No tiene
    checkout ni webhook propios; solo implementa `cancel_subscription` para
    que "cancelar" funcione igual sin importar el proveedor.
    """

    code = "manual"

    def create_checkout(self, *, user, plan_price, subscription, success_url, cancel_url):
        raise NotImplementedError(
            "El plan manual no tiene checkout -- se otorga a mano desde /admin/."
        )

    def verify_webhook(self, request) -> bool:
        return False

    def parse_webhook_event(self, request) -> WebhookEvent:
        raise NotImplementedError("El proveedor manual no recibe webhooks.")

    def cancel_subscription(self, subscription) -> None:
        from django.utils import timezone

        subscription.canceled_at = timezone.now()
        # Con fecha de vencimiento, "cancelar" es "no renovar" -- el status
        # queda como está (activa/en gracia) para que `is_in_force` siga
        # dando acceso hasta esa fecha, tal como promete la pantalla de Pro
        # ("seguís teniendo acceso hasta que termine el período ya pagado").
        # Sin fecha (alta indefinida: grandfather, promo/trial sin
        # duración) no hay período que esperar -- ahí sí corta ya mismo.
        # HALLAZGO: antes esta rama no existía y SIEMPRE cortaba al toque,
        # sin importar `current_period_end` -- contradecía ese mismo texto.
        if subscription.current_period_end is None:
            subscription.status = subscription.STATUS_CANCELED
            subscription.save(update_fields=["status", "canceled_at", "updated_at"])
        else:
            subscription.save(update_fields=["canceled_at", "updated_at"])


class WompiProvider(PaymentProvider):
    """
    ESQUELETO SIN VERIFICAR. La forma general (enlace de pago hosteado +
    notificación por webhook) surge de la documentación pública de Wompi,
    pero el shape exacto del payload y el algoritmo de firma NO están
    confirmados todavía -- hace falta acceso a docs.wompi.sv con
    credenciales reales (o al sandbox) para completar los tres métodos de
    abajo. Hasta entonces, cualquier intento de checkout/webhook con este
    proveedor falla con un error claro en vez de fallar silenciosamente o
    inventar un comportamiento.

    Cuando se complete, `create_checkout` debe pasar
    `subscription.checkout_reference` como referencia/metadata del enlace
    de pago para que `parse_webhook_event` la pueda leer de vuelta y
    devolverla en `WebhookEvent.reference` -- así se correlaciona el
    webhook con esta fila incluso antes de conocer el id de suscripción de
    Wompi (ver `services.apply_webhook_event`).
    """

    code = "wompi"

    def __init__(self):
        self.api_key = settings.WOMPI_API_KEY
        self.webhook_secret = settings.WOMPI_WEBHOOK_SECRET

    def create_checkout(self, *, user, plan_price, subscription, success_url, cancel_url):
        raise NotImplementedError(
            "Falta conectar contra la API real de Wompi (crear el enlace de pago) -- "
            "pendiente de credenciales/sandbox, ver docs.wompi.sv."
        )

    def verify_webhook(self, request) -> bool:
        raise NotImplementedError(
            "Falta confirmar el algoritmo de firma real de Wompi para webhooks."
        )

    def parse_webhook_event(self, request) -> WebhookEvent:
        raise NotImplementedError(
            "Falta confirmar el shape real del payload de webhook de Wompi."
        )

    def cancel_subscription(self, subscription) -> None:
        raise NotImplementedError(
            "Falta el endpoint real de cancelación de Wompi."
        )


_PROVIDERS: dict[str, type[PaymentProvider]] = {
    ManualProvider.code: ManualProvider,
    WompiProvider.code: WompiProvider,
}


def get_provider(code: str) -> PaymentProvider:
    try:
        return _PROVIDERS[code]()
    except KeyError:
        raise ValueError(f"Proveedor de pago desconocido: {code!r}")
