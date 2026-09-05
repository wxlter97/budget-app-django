"""
Conversión a la moneda base del workspace, a partir de las tasas manuales
guardadas en ``ExchangeRate``. Sin API externa: el usuario las carga/edita
a mano (Herramientas → Monedas en la app).
"""
from decimal import Decimal

from .models import ExchangeRate, Workspace


def get_rate_map(workspace: Workspace) -> dict[str, Decimal]:
    """``{moneda: tasa_a_base}``, con la moneda base del workspace en 1."""
    rates = {
        r.currency: r.rate_to_base
        for r in ExchangeRate.objects.filter(workspace=workspace)
    }
    rates[workspace.base_currency] = Decimal("1")
    return rates


def convert(amount: Decimal, currency: str, rate_map: dict[str, Decimal]) -> Decimal | None:
    """``None`` si `currency` no es la base y no tiene tasa configurada --
    el llamador decide qué hacer (en los reportes: se excluye del total)."""
    rate = rate_map.get(currency)
    return None if rate is None else amount * rate
