"""Genera un par de claves VAPID nuevo para Web Push (ver
`apps.notifications.services._send_web_push`) e imprime las líneas para
pegar en `.env`. No las guarda en ningún lado -- si ya hay dispositivos web
registrados contra el par viejo, cambiarlo los invalida (tienen que
re-suscribirse), así que esto es deliberadamente manual.
"""
from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode


class Command(BaseCommand):
    help = "Genera un par de claves VAPID nuevo para Web Push (VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY)."

    def handle(self, *args, **options):
        vapid = Vapid02()
        vapid.generate_keys()

        private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
        public_raw = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )

        self.stdout.write("Agregá esto a tu .env:\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={b64urlencode(public_raw)}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={b64urlencode(private_raw)}")
        self.stdout.write(
            self.style.WARNING(
                "\nGuardá la privada en un lugar seguro (no se puede recuperar) -- "
                "y no la commitees. Cambiar el par invalida las suscripciones web "
                "ya registradas: van a tener que reactivar los avisos push."
            )
        )
