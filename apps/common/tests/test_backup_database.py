"""
`manage.py backup_database` — ver RUNBOOK.md.

No se llama a `pg_dump` de verdad: los tests corren con SQLite en memoria
(`config/test_settings.py`) y el punto de este comando no es el formato del
volcado sino todo lo que lo rodea — que no se haga trabajo al pedo cuando no
hay dónde guardar, que la contraseña no termine en la línea de comandos, que
un `pg_dump` fallido se note, y que la retención no deje el bucket vacío.
"""
import datetime as dt
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

_MODULE = "apps.common.management.commands.backup_database"

_PG = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "budget",
        "USER": "budget_owner",
        "PASSWORD": "no-debe-aparecer-en-argv",
        "HOST": "ep-ejemplo-pooler.us-east-2.aws.neon.tech",
        "PORT": "5432",
        "OPTIONS": {"sslmode": "require"},
    }
}


def _fake_pg_dump(cmd, env=None, **kwargs):
    """Escribe un archivo con contenido en `--file=` y sale bien, como pg_dump."""
    target = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--file="))
    Path(target).write_bytes(b"PGDMP" + b"\0" * 1024)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _blob(name, days_old):
    blob = MagicMock()
    blob.name = name
    blob.time_created = timezone.now() - dt.timedelta(days=days_old)
    return blob


@override_settings(DB_BACKUP_BUCKET="", DATABASES=_PG)
class SinBucketTests(SimpleTestCase):
    databases = []

    def test_avisa_y_no_gasta_un_pg_dump_que_va_a_tirar(self):
        out = StringIO()
        with patch(f"{_MODULE}.subprocess.run") as run:
            call_command("backup_database", stdout=out)
        run.assert_not_called()
        self.assertIn("AVISO", out.getvalue())

    def test_con_to_igual_vuelca_aunque_no_haya_bucket(self):
        with patch(f"{_MODULE}.subprocess.run", side_effect=_fake_pg_dump):
            with tempfile.TemporaryDirectory() as tmp:
                call_command("backup_database", to=tmp, stdout=StringIO())
                dumps = list(Path(tmp).glob("budget-*.dump"))
        self.assertEqual(len(dumps), 1)


@override_settings(
    DB_BACKUP_BUCKET="budget-backups",
    DB_BACKUP_PREFIX="backups/db/",
    DB_BACKUP_RETENTION_DAYS=0,
    DATABASES=_PG,
)
class VolcadoYSubidaTests(SimpleTestCase):
    databases = []

    def test_arma_el_pg_dump_sin_filtrar_la_contrasena_en_los_argumentos(self):
        with patch(f"{_MODULE}.subprocess.run", side_effect=_fake_pg_dump) as run:
            with patch(f"{_MODULE}.Command._client"):
                call_command("backup_database", stdout=StringIO())

        cmd, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(cmd[0], "pg_dump")
        self.assertIn("--format=custom", cmd)
        self.assertIn("--no-owner", cmd)
        self.assertIn("--host=ep-ejemplo-pooler.us-east-2.aws.neon.tech", cmd)
        self.assertIn("--username=budget_owner", cmd)
        self.assertEqual(cmd[-1], "budget")
        # La contraseña va por el entorno: los argumentos de un proceso los ve
        # cualquiera que liste procesos en la máquina.
        self.assertNotIn("no-debe-aparecer-en-argv", " ".join(cmd))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "no-debe-aparecer-en-argv")
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")

    def test_sube_al_prefijo_configurado_con_el_nombre_fechado(self):
        client = MagicMock()
        with patch(f"{_MODULE}.subprocess.run", side_effect=_fake_pg_dump):
            with patch(f"{_MODULE}.Command._client", return_value=client):
                call_command("backup_database", stdout=StringIO())

        client.bucket.assert_called_once_with("budget-backups")
        blob_name = client.bucket.return_value.blob.call_args[0][0]
        self.assertTrue(blob_name.startswith("backups/db/budget-"))
        self.assertTrue(blob_name.endswith(".dump"))
        client.bucket.return_value.blob.return_value.upload_from_filename.assert_called_once()

    def test_un_pg_dump_fallido_revienta_con_el_stderr_a_la_vista(self):
        fallo = subprocess.CompletedProcess(
            ["pg_dump"], 1, "", "pg_dump: error: server version: 17.2; pg_dump version: 15.8"
        )
        with patch(f"{_MODULE}.subprocess.run", return_value=fallo):
            with self.assertRaises(CommandError) as ctx:
                call_command("backup_database", stdout=StringIO())
        self.assertIn("server version", str(ctx.exception))

    def test_sin_pg_dump_instalado_el_mensaje_dice_donde_se_instala(self):
        with patch(f"{_MODULE}.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(CommandError) as ctx:
                call_command("backup_database", stdout=StringIO())
        self.assertIn("postgresql-client", str(ctx.exception))


@override_settings(
    DB_BACKUP_BUCKET="budget-backups",
    DB_BACKUP_PREFIX="backups/db/",
    DB_BACKUP_RETENTION_DAYS=30,
    DATABASES=_PG,
)
class RetencionTests(SimpleTestCase):
    databases = []

    def _correr(self, blobs):
        client = MagicMock()
        client.bucket.return_value.list_blobs.return_value = blobs
        with patch(f"{_MODULE}.subprocess.run", side_effect=_fake_pg_dump):
            with patch(f"{_MODULE}.Command._client", return_value=client):
                call_command("backup_database", stdout=StringIO())
        return client

    def test_borra_los_volcados_vencidos(self):
        viejo, reciente = _blob("backups/db/viejo.dump", 90), _blob("backups/db/hoy.dump", 0)
        self._correr([reciente, viejo])
        viejo.delete.assert_called_once()
        reciente.delete.assert_not_called()

    def test_nunca_borra_el_ultimo_que_queda(self):
        """Si el job estuvo caído un mes, lo que hay es viejo pero es todo lo
        que hay: borrarlo por la fecha vaciaría el bucket justo cuando más
        falta hace."""
        unico = _blob("backups/db/viejo.dump", 400)
        self._correr([unico])
        unico.delete.assert_not_called()

    def test_keep_days_cero_no_borra_nada(self):
        viejo = _blob("backups/db/viejo.dump", 400)
        otro = _blob("backups/db/menos-viejo.dump", 300)
        client = MagicMock()
        client.bucket.return_value.list_blobs.return_value = [viejo, otro]
        with patch(f"{_MODULE}.subprocess.run", side_effect=_fake_pg_dump):
            with patch(f"{_MODULE}.Command._client", return_value=client):
                call_command("backup_database", keep_days=0, stdout=StringIO())
        viejo.delete.assert_not_called()
        otro.delete.assert_not_called()
