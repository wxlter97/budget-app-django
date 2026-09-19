"""
Vuelca la base con `pg_dump` y sube el archivo a Google Cloud Storage.

Por qué existe: Neon en plan free retiene ~24 h de historial. Eso cubre el
"borré algo hace un rato", no el "nos dimos cuenta el lunes". Un volcado
diario a GCS cuesta centavos y es lo único que permite volver a un estado de
la semana pasada.

    python manage.py backup_database            # vuelca y sube a GCS
    python manage.py backup_database --to /tmp  # vuelca a un archivo local

Corre solo, todos los días, como primer paso de `run_daily_tasks` (ver
DEPLOY.md §6). Si no hay bucket configurado avisa y no hace nada — el aviso
sale en los logs de Cloud Run y `common.W003` lo repite en cada arranque.

Formato: `--format=custom` (comprimido, y permite restaurar tablas sueltas con
`pg_restore -t`). Restaurar está documentado en RUNBOOK.md.

El volcado se escribe primero a un archivo temporal y recién después se sube,
para no dejar un objeto truncado en el bucket si `pg_dump` se cae a la mitad.
En Cloud Run ese temporal vive en `/tmp`, que es RAM: el comando registra el
tamaño en cada corrida justo para que se vea venir si algún día se acerca a la
memoria de la instancia.
"""
import datetime as dt
import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

# `pg_dump` se niega a volcar un servidor de una versión mayor que la suya, así
# que la imagen tiene que traer un cliente al día (ver Dockerfile). Es el modo
# más común de que esto falle, y el mensaje de pg_dump lo dice con todas las
# letras, así que se propaga tal cual en vez de resumirlo.
_DUMP_SUFFIX = ".dump"


class Command(BaseCommand):
    help = "Vuelca la base con pg_dump y sube el archivo a GCS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            metavar="DIRECTORIO",
            help=(
                "Guarda el volcado en este directorio local en vez de subirlo. "
                "Para backups a mano y para probar el comando sin GCS."
            ),
        )
        parser.add_argument(
            "--keep-days",
            type=int,
            default=None,
            help=(
                "Sobrescribe DB_BACKUP_RETENTION_DAYS para esta corrida. "
                "0 = no borrar nada."
            ),
        )

    def handle(self, *args, **options):
        bucket = settings.DB_BACKUP_BUCKET
        if not bucket and not options["to"]:
            # Antes del volcado y no después: no tiene sentido gastar el
            # pg_dump para tirar el archivo. Es también lo que hace que el
            # comando sea inofensivo en desarrollo y en los tests.
            self.stdout.write(
                self.style.WARNING(
                    "AVISO: no hay DB_BACKUP_BUCKET ni GS_BUCKET_NAME, así que no se "
                    "hace backup. La base queda sin más respaldo que las ~24 h de "
                    "historial de Neon (ver CONFIG-PENDIENTE.md)."
                )
            )
            return

        db = settings.DATABASES["default"]
        if "postgresql" not in db["ENGINE"]:
            raise CommandError(
                f"backup_database sólo sabe volcar PostgreSQL (ENGINE={db['ENGINE']})."
            )

        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        filename = f"budget-{stamp}{_DUMP_SUFFIX}"

        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(options["to"]) if options["to"] else Path(tmp)
            if options["to"]:
                local_dir.mkdir(parents=True, exist_ok=True)
            path = local_dir / filename

            self.stdout.write(f"→ pg_dump de {db['NAME']}...")
            self._pg_dump(db, path)
            size_mb = path.stat().st_size / (1024 * 1024)
            self.stdout.write(f"  volcado: {path.name} ({size_mb:.1f} MB)")

            if options["to"]:
                self.stdout.write(self.style.SUCCESS(f"Listo: {path}"))
                return

            blob_name = f"{settings.DB_BACKUP_PREFIX}{filename}"
            self._upload(bucket, blob_name, path)
            self.stdout.write(self.style.SUCCESS(f"Listo: gs://{bucket}/{blob_name}"))

        keep_days = options["keep_days"]
        if keep_days is None:
            keep_days = settings.DB_BACKUP_RETENTION_DAYS
        if keep_days:
            self._prune(bucket, keep_days)

    # -- pg_dump ------------------------------------------------------------

    def _pg_dump(self, db, path):
        cmd = [
            "pg_dump",
            "--format=custom",
            # El rol de la app no es el dueño de los objetos en Neon, y al
            # restaurar en otra base tampoco va a existir: sin esto pg_restore
            # se llena de errores de "role does not exist" que no importan.
            "--no-owner",
            "--no-privileges",
            f"--file={path}",
            f"--host={db['HOST'] or 'localhost'}",
            f"--port={db['PORT'] or 5432}",
            f"--username={db['USER']}",
            db["NAME"],
        ]
        env = {**os.environ}
        if db.get("PASSWORD"):
            # Por variable de entorno y no en la línea de comandos: los args de
            # un proceso los ve cualquiera que liste procesos.
            env["PGPASSWORD"] = db["PASSWORD"]
        if db.get("OPTIONS", {}).get("sslmode"):
            env["PGSSLMODE"] = db["OPTIONS"]["sslmode"]

        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise CommandError(
                "No hay `pg_dump` en el PATH. En la imagen lo instala el "
                "Dockerfile (postgresql-client); en local, el paquete "
                "postgresql-client de tu distro."
            ) from exc

        if proc.returncode != 0:
            raise CommandError(f"pg_dump falló (código {proc.returncode}):\n{proc.stderr.strip()}")

    # -- GCS ----------------------------------------------------------------

    def _client(self):
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - dependencia de prod
            raise CommandError(
                "Falta google-cloud-storage (viene con django-storages[google])."
            ) from exc
        return storage.Client()

    def _upload(self, bucket_name, blob_name, path):
        blob = self._client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(str(path), content_type="application/octet-stream")

    def _prune(self, bucket_name, keep_days):
        """Borra los volcados más viejos que `keep_days`, pero nunca el último.

        Lo de "nunca el último" es a propósito: si el job estuvo caído un mes,
        lo que queda es viejo pero es todo lo que hay, y borrarlo por la fecha
        dejaría el bucket vacío justo cuando más falta hace.
        """
        cutoff = timezone.now() - dt.timedelta(days=keep_days)
        bucket = self._client().bucket(bucket_name)
        blobs = sorted(
            bucket.list_blobs(prefix=settings.DB_BACKUP_PREFIX),
            key=lambda b: b.time_created,
        )
        stale = [b for b in blobs[:-1] if b.time_created < cutoff]
        for blob in stale:
            blob.delete()
        if stale:
            self.stdout.write(f"  borrados {len(stale)} volcados de más de {keep_days} días")
