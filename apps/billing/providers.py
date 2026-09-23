"""
Capa de abstracción de proveedor de pago.

El resto de `apps.billing` (vistas, servicios) no debe importar nada
específico de Wompi -- solo esta interfaz. Sumar un proveedor nuevo (o
reemplazar Wompi el día de mañana) es agregar una clase acá y registrarla
en `_PROVIDERS`, sin tocar modelos ni vistas.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


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
    # Id único del aviso en el proveedor (p. ej. el de la transacción): permite ignorar un
    # reintento del mismo aviso en vez de aplicarlo dos veces.
    event_id: str = ""
    # Monto cobrado, si el proveedor lo informa: se compara con el precio antes de activar.
    amount: Decimal | None = None
    raw: dict = dataclasses.field(default_factory=dict)


class PaymentProvider:
    """Interfaz que implementa cada proveedor concreto."""

    code: str
    # Si el precio tiene que estar dado de alta de antemano en el proveedor (con su id en
    # `PlanPrice.external_refs`). Wompi crea el enlace en cada compra: no lo necesita.
    needs_external_ref = True

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
    needs_external_ref = False

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


class WompiError(Exception):
    """Wompi respondió con un error, no contestó, o mandó algo que no entendemos."""


# Token OAuth en memoria del proceso: dura ~1 h (`expires_in`) y la propia documentación
# pide no pedir uno por cada llamada.
_token_cache: dict = {"value": "", "expires_at": 0.0}


class WompiProvider(PaymentProvider):
    """
    Wompi El Salvador (docs.wompi.sv).

    - Autenticación: OAuth 2.0 *client credentials* con el App ID y el API Secret del negocio.
    - Plan **mensual**: un `EnlacePagoRecurrente` *por compra* (no uno compartido por plan).
      Así el enlace corresponde a una sola persona: su `idEnlace` queda en
      `Subscription.external_subscription_id` y cancelar es desactivar ese enlace. El día de
      cobro es el día de la compra (tope 28). Wompi no acepta una referencia nuestra en estos
      enlaces, por eso la referencia va en el texto del producto y la correspondencia real es
      el id del enlace.
    - Plan **anual / de por vida**: un `EnlacePago` único con `identificadorEnlaceComercio` =
      nuestra `checkout_reference`, que vuelve en el webhook. Se renueva pagando otro enlace.
    - Webhook: firmado con `wompi_hash` = HMAC-SHA256 hex del cuerpo crudo con el API Secret.
      Sólo avisa de cobros exitosos: un cobro recurrente fallido no manda nada, y la
      suscripción simplemente deja de renovarse y vence sola.

    Lo que la documentación no dice (primer cobro, forma exacta del aviso de un cobro
    recurrente) se descubre en el sandbox: `manage.py wompi_probe` y `WOMPI_LOG_WEBHOOKS`.
    """

    code = "wompi"
    needs_external_ref = False
    TIMEOUT = 15

    def __init__(self):
        # `.strip()`: un espacio o salto de línea de más al crear el secret (fácil al
        # copiar del panel de Wompi) da un 403/401 sin explicación mejor -- ver
        # `_token`, que ahora sí muestra lo que Wompi contesta.
        self.client_id = settings.WOMPI_CLIENT_ID.strip()
        self.client_secret = settings.WOMPI_CLIENT_SECRET.strip()

    # -- HTTP -----------------------------------------------------------------
    def _relay_headers(self) -> dict:
        return {"X-Relay-Secret": settings.WOMPI_RELAY_SECRET} if settings.WOMPI_RELAY_URL else {}

    def _auth_url(self) -> str:
        """URL real de `id.wompi.sv/connect/token`, o su equivalente detrás del relay
        (`{WOMPI_RELAY_URL}/id/connect/token`) cuando hay uno configurado."""
        if not settings.WOMPI_RELAY_URL:
            return settings.WOMPI_AUTH_URL
        path = settings.WOMPI_AUTH_URL.split("id.wompi.sv", 1)[1]
        return f"{settings.WOMPI_RELAY_URL.rstrip('/')}/id{path}"

    def _api_url(self, path: str) -> str:
        """URL real de `api.wompi.sv{path}`, o su equivalente detrás del relay
        (`{WOMPI_RELAY_URL}/api{path}`) cuando hay uno configurado."""
        if not settings.WOMPI_RELAY_URL:
            return f"{settings.WOMPI_API_URL}{path}"
        return f"{settings.WOMPI_RELAY_URL.rstrip('/')}/api{path}"

    def _token(self) -> str:
        if _token_cache["value"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["value"]
        if not (self.client_id and self.client_secret):
            raise WompiError("Faltan WOMPI_CLIENT_ID / WOMPI_CLIENT_SECRET.")
        try:
            res = requests.post(
                self._auth_url(),
                data={
                    "grant_type": "client_credentials",
                    "audience": "wompi_api",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers=self._relay_headers(),
                timeout=self.TIMEOUT,
            )
        except requests.RequestException as exc:
            raise WompiError(f"No se pudo contactar a Wompi para autenticar: {exc}") from exc
        if res.status_code != 200:
            raise WompiError(
                f"Wompi rechazó las credenciales ({res.status_code}): {res.text[:500]}"
            )
        try:
            data = res.json()
        except ValueError as exc:
            raise WompiError(f"Wompi no devolvió JSON al autenticar: {res.text[:300]}") from exc
        token = data.get("access_token")
        if not token:
            raise WompiError("Wompi no devolvió un access_token.")
        # Un minuto de margen para no usar un token que caduca a mitad de la llamada.
        _token_cache["value"] = token
        _token_cache["expires_at"] = time.time() + max(int(data.get("expires_in", 3600)) - 60, 0)
        return token

    def request_api(self, method: str, path: str, payload: dict | None = None):
        """Llama a la API de Wompi con el token vigente. Devuelve el JSON (o None si no hay cuerpo)."""
        try:
            res = requests.request(
                method,
                self._api_url(path),
                json=payload,
                headers={"authorization": f"Bearer {self._token()}", **self._relay_headers()},
                timeout=self.TIMEOUT,
            )
        except requests.RequestException as exc:
            raise WompiError(f"No se pudo contactar a Wompi: {exc}") from exc
        if res.status_code == 401:
            _token_cache["value"] = ""  # el token pudo haber caducado antes de lo previsto
        if not 200 <= res.status_code < 300:
            raise WompiError(f"Wompi respondió {res.status_code}: {res.text[:300]}")
        if not res.content:
            return None
        try:
            return res.json()
        except ValueError as exc:
            raise WompiError("Wompi respondió algo que no es JSON.") from exc

    # -- PaymentProvider ------------------------------------------------------
    def create_checkout(self, *, user, plan_price, subscription, success_url, cancel_url):
        reference = str(subscription.checkout_reference)
        # No siempre es el precio: un plan de por vida con crédito de prorrateo cobra menos.
        amount = round(subscription.charge_cents / 100, 2)
        plan_name = plan_price.plan.name

        if plan_price.billing_period == plan_price.BILLING_MONTHLY:
            data = self.request_api("POST", "/EnlacePagoRecurrente", {
                "diaDePago": min(timezone.localdate().day, 28),
                "nombre": f"{plan_name} · mensual",
                "idAplicativo": self.client_id,
                "monto": amount,
                "descripcionProducto": f"Suscripción mensual a {plan_name} (ref. {reference})",
            })
        else:
            # Monto y cantidad NO editables: si no, se podría pagar menos que el precio.
            config = {
                "urlRedirect": success_url, "urlRetorno": cancel_url,
                "esMontoEditable": False, "esCantidadEditable": False,
            }
            if settings.WOMPI_WEBHOOK_URL:
                config["urlWebhook"] = settings.WOMPI_WEBHOOK_URL
            data = self.request_api("POST", "/EnlacePago", {
                "identificadorEnlaceComercio": reference,
                "monto": amount,
                "nombreProducto": f"{plan_name} · {plan_price.get_billing_period_display().lower()}",
                "configuracion": config,
            })

        url = (data or {}).get("urlEnlace") or (data or {}).get("urlEnlaceLargo")
        if not url:
            raise WompiError("Wompi no devolvió la URL del enlace de pago.")
        subscription.external_subscription_id = str((data or {}).get("idEnlace", ""))
        subscription.save(update_fields=["external_subscription_id", "updated_at"])
        return CheckoutSession(checkout_url=url)

    def verify_webhook(self, request) -> bool:
        signature = request.META.get("HTTP_WOMPI_HASH", "").strip().lower()
        if not signature or not self.client_secret:
            return False
        expected = hmac.new(
            self.client_secret.encode(), request.body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(self, request) -> WebhookEvent:
        if settings.WOMPI_LOG_WEBHOOKS:
            logger.warning("Webhook de Wompi: %s", request.body.decode("utf-8", "replace"))
        try:
            body = json.loads(request.body)
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        approved = str(body.get("ResultadoTransaccion", "")).lower().startswith("exitosa")
        productive = body.get("EsProductiva", body.get("esProductiva"))
        is_test = productive is False
        if not approved or (is_test and not settings.WOMPI_ACCEPT_TEST_PAYMENTS):
            return WebhookEvent(kind="payment.ignored", raw=body)

        try:
            amount = Decimal(str(body["Monto"]))
        except (KeyError, InvalidOperation):
            amount = None

        link = body.get("EnlacePago") or {}
        return WebhookEvent(
            kind="subscription.activated",  # activar y renovar se aplican igual
            reference=str(link.get("IdentificadorEnlaceComercio") or ""),
            external_customer_id=str((body.get("cliente") or {}).get("Email") or ""),
            event_id=str(body.get("IdTransaccion") or ""),
            amount=amount,
            raw=body,
        )

    def cancel_subscription(self, subscription) -> None:
        plan_price = subscription.plan_price
        recurring = plan_price is not None and plan_price.billing_period == plan_price.BILLING_MONTHLY
        if recurring and subscription.external_subscription_id:
            # Desactiva el enlace recurrente de esta persona: no se le cobra el mes siguiente.
            self.request_api("POST", f"/EnlacePagoRecurrente/{subscription.external_subscription_id}")
        # Con o sin enlace, el acceso sigue hasta el fin del período ya pagado.
        ManualProvider().cancel_subscription(subscription)


_PROVIDERS: dict[str, type[PaymentProvider]] = {
    ManualProvider.code: ManualProvider,
    WompiProvider.code: WompiProvider,
}


def get_provider(code: str) -> PaymentProvider:
    try:
        return _PROVIDERS[code]()
    except KeyError:
        raise ValueError(f"Proveedor de pago desconocido: {code!r}")
