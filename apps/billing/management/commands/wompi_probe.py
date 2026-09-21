"""Prueba la conexión con Wompi (sandbox o producción, según las credenciales y URLs).

Sirve para descubrir lo que la documentación no dice, sin pasar por la app:

    manage.py wompi_probe                          # sólo pide el token: ¿sirven las credenciales?
    manage.py wompi_probe --pago 1.00              # crea un enlace de pago único
    manage.py wompi_probe --recurrente 0.99        # crea un enlace de cobro recurrente
    manage.py wompi_probe --suscriptores ID        # quién se suscribió a un enlace recurrente
    manage.py wompi_probe --desactivar ID          # desactiva un enlace recurrente

No toca la base de datos. Cada enlace creado queda en el panel de Wompi: al terminar,
desactivar los de prueba.
"""
import json
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing.providers import WompiError, WompiProvider


class Command(BaseCommand):
    help = "Prueba las credenciales de Wompi y crea enlaces de prueba."

    def add_arguments(self, parser):
        parser.add_argument("--pago", type=float, metavar="MONTO", help="Crea un enlace de pago único.")
        parser.add_argument("--recurrente", type=float, metavar="MONTO", help="Crea un enlace recurrente.")
        parser.add_argument("--suscriptores", metavar="ID", help="Lista los suscritos a un enlace recurrente.")
        parser.add_argument("--desactivar", metavar="ID", help="Desactiva un enlace recurrente.")

    def _show(self, title, data):
        self.stdout.write(self.style.SUCCESS(title))
        self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def handle(self, *args, **options):
        provider = WompiProvider()
        self.stdout.write(f"API: {settings.WOMPI_API_URL}")
        try:
            provider._token()
            self.stdout.write(self.style.SUCCESS("Token OAuth obtenido: las credenciales sirven."))

            if options["pago"] is not None:
                config = {}
                if settings.WOMPI_WEBHOOK_URL:
                    config["urlWebhook"] = settings.WOMPI_WEBHOOK_URL
                data = provider.request_api("POST", "/EnlacePago", {
                    "identificadorEnlaceComercio": f"probe-{uuid.uuid4()}",
                    "monto": options["pago"],
                    "nombreProducto": "Prueba de integración",
                    "configuracion": config,
                })
                self._show("Enlace de pago creado (ábrelo y paga con una tarjeta de prueba):", data)

            if options["recurrente"] is not None:
                data = provider.request_api("POST", "/EnlacePagoRecurrente", {
                    "diaDePago": min(timezone.localdate().day, 28),
                    "nombre": "Prueba de integración · mensual",
                    "idAplicativo": settings.WOMPI_CLIENT_ID,
                    "monto": options["recurrente"],
                    "descripcionProducto": f"Prueba de cobro recurrente (ref. probe-{uuid.uuid4()})",
                })
                self._show("Enlace recurrente creado (ábrelo y afíliate):", data)

            if options["suscriptores"]:
                data = provider.request_api(
                    "GET", f"/EnlacePagoRecurrente/{options['suscriptores']}/suscrpciones"
                )
                self._show("Suscritos:", data)

            if options["desactivar"]:
                data = provider.request_api("POST", f"/EnlacePagoRecurrente/{options['desactivar']}")
                self._show("Enlace desactivado:", data)
        except WompiError as exc:
            raise CommandError(str(exc))
