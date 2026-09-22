"""Prueba la conexión con Wompi (sandbox o producción, según las credenciales y URLs).

Sirve para descubrir lo que la documentación no dice, sin pasar por la app:

    manage.py wompi_probe                          # sólo pide el token: ¿sirven las credenciales?
    manage.py wompi_probe --diagnostico-red         # compara requests (Python) vs curl, mismo pedido
    manage.py wompi_probe --pago 1.00              # crea un enlace de pago único
    manage.py wompi_probe --recurrente 0.99        # crea un enlace de cobro recurrente
    manage.py wompi_probe --suscriptores ID        # quién se suscribió a un enlace recurrente
    manage.py wompi_probe --desactivar ID          # desactiva un enlace recurrente

No toca la base de datos. Cada enlace creado queda en el panel de Wompi: al terminar,
desactivar los de prueba.
"""
import json
import subprocess
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing.providers import WompiError, WompiProvider


def curl_post_form(url: str, data: dict, timeout: int = 15) -> tuple[str | None, str]:
    """
    El mismo POST que `requests`, pero con el motor TLS de `curl` (libcurl/OpenSSL) en vez
    del de Python (urllib3) -- si un firewall filtra por la "huella" del cliente HTTP y no
    sólo por IP, acá se nota sin tener que cambiar nada de infraestructura. Manda el cuerpo
    por stdin (`--data-binary @-`) para que no quede en la lista de procesos del contenedor.

    Devuelve (código de estado o None si curl no corrió, cuerpo recortado).
    """
    body = "&".join(f"{k}={v}" for k, v in data.items())
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "-X", "POST", url,
             "--data-binary", "@-", "-H", "Content-Type: application/x-www-form-urlencoded"],
            input=body, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        return None, f"no se pudo correr curl: {exc}"
    if proc.returncode != 0:
        return None, f"curl terminó con error ({proc.returncode}): {proc.stderr[:300]}"
    *body_lines, code = proc.stdout.rsplit("\n", 1)
    return code.strip(), "\n".join(body_lines)[:400]


class Command(BaseCommand):
    help = "Prueba las credenciales de Wompi y crea enlaces de prueba."

    def add_arguments(self, parser):
        parser.add_argument(
            "--diagnostico-red", action="store_true",
            help="Pide el token con requests y con curl, y compara -- para saber si un "
            "bloqueo es por IP (falla con los dos) o por el cliente HTTP (sólo con uno).",
        )
        parser.add_argument("--pago", type=float, metavar="MONTO", help="Crea un enlace de pago único.")
        parser.add_argument("--recurrente", type=float, metavar="MONTO", help="Crea un enlace recurrente.")
        parser.add_argument("--suscriptores", metavar="ID", help="Lista los suscritos a un enlace recurrente.")
        parser.add_argument("--desactivar", metavar="ID", help="Desactiva un enlace recurrente.")

    def _show(self, title, data):
        self.stdout.write(self.style.SUCCESS(title))
        self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def _diagnostico_red(self):
        if not (settings.WOMPI_CLIENT_ID and settings.WOMPI_CLIENT_SECRET):
            raise CommandError("Faltan WOMPI_CLIENT_ID / WOMPI_CLIENT_SECRET.")
        form = {
            "grant_type": "client_credentials", "audience": "wompi_api",
            "client_id": settings.WOMPI_CLIENT_ID.strip(), "client_secret": settings.WOMPI_CLIENT_SECRET.strip(),
        }

        self.stdout.write(f"Probando {settings.WOMPI_AUTH_URL} con dos clientes HTTP distintos...\n")

        import requests
        try:
            res = requests.post(settings.WOMPI_AUTH_URL, data=form, timeout=15)
            py_code, py_body = str(res.status_code), res.text[:400]
        except requests.RequestException as exc:
            py_code, py_body = None, str(exc)
        self.stdout.write(f"requests (Python): {py_code}\n{py_body}\n")

        curl_code, curl_body = curl_post_form(settings.WOMPI_AUTH_URL, form)
        self.stdout.write(f"\ncurl: {curl_code}\n{curl_body}\n")

        proxy_code = None
        if settings.WOMPI_PROXY_URL:
            try:
                res = requests.post(
                    settings.WOMPI_AUTH_URL, data=form, timeout=15,
                    proxies={"http": settings.WOMPI_PROXY_URL, "https": settings.WOMPI_PROXY_URL},
                )
                proxy_code, proxy_body = str(res.status_code), res.text[:400]
            except requests.RequestException as exc:
                proxy_code, proxy_body = None, str(exc)
            self.stdout.write(f"\nrequests vía WOMPI_PROXY_URL: {proxy_code}\n{proxy_body}\n")

        py_blocked = py_code != "200" and "Application-Gateway" in (py_body or "")
        curl_blocked = curl_code != "200" and "Application-Gateway" in (curl_body or "")
        self.stdout.write("\n" + self.style.WARNING("Conclusión:"))
        if py_blocked and curl_blocked:
            self.stdout.write("Los dos clientes fueron bloqueados: no es la huella del cliente HTTP, "
                              "el firewall filtra por esta IP/red.")
        elif py_blocked and not curl_blocked:
            self.stdout.write("Sólo requests fue bloqueado: podría ser la huella TLS del cliente "
                              "Python, no la IP. Vale la pena que el proveedor pruebe con curl igual.")
        elif not py_blocked and not curl_blocked:
            self.stdout.write("Ninguno fue bloqueado en esta corrida.")
        else:
            self.stdout.write("Resultado mixto e inesperado -- revisar los cuerpos de arriba.")
        if settings.WOMPI_PROXY_URL:
            if proxy_code == "200":
                self.stdout.write(self.style.SUCCESS("El relay (WOMPI_PROXY_URL) SÍ pasa: sirve."))
            else:
                self.stdout.write(self.style.ERROR("El relay (WOMPI_PROXY_URL) también fue bloqueado o falló."))

    def handle(self, *args, **options):
        if options["diagnostico_red"]:
            self._diagnostico_red()
            return

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
