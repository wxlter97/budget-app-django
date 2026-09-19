"""El archivo del recibo se va del storage cuando la transacción se borra de verdad.

Django no borra archivos al borrar filas, así que sin el receptor de
`signals._delete_receipt_file` cada borrado físico deja la foto huérfana en el
bucket: se sigue pagando y ya no hay forma de verla. El caso que más pesa no es
el borrado de a una sino la cascada (`wipe_workspace_data`, que es lo que corre al
vaciar un workspace o al restaurar un backup).
"""
import datetime as dt
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import wipe_workspace_data

User = get_user_model()

MEDIA_ROOT = tempfile.mkdtemp(prefix="budget-test-cleanup-")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class BorradoDelRecibo(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.category = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def _con_recibo(self):
        txn = Transaction.objects.create(
            wallet=self.wallet, category=self.category, amount=10, date=dt.date(2026, 9, 1)
        )
        txn.receipt.save("recibo.png", SimpleUploadedFile("recibo.png", b"png"), save=True)
        path = Path(MEDIA_ROOT) / txn.receipt.name
        self.assertTrue(path.exists())
        return txn, path

    def test_el_borrado_fisico_se_lleva_el_archivo(self):
        txn, path = self._con_recibo()
        txn.delete()
        self.assertFalse(path.exists())

    def test_la_cascada_de_purge_workspace_tambien(self):
        _, path = self._con_recibo()
        wipe_workspace_data(self.ws)
        self.assertFalse(path.exists())

    def test_el_soft_delete_conserva_el_archivo(self):
        # La transacción se puede recuperar, así que el recibo tiene que seguir ahí.
        txn, path = self._con_recibo()
        txn.soft_delete()
        self.assertTrue(path.exists())

    def test_una_transaccion_sin_recibo_no_toca_el_storage(self):
        txn = Transaction.objects.create(
            wallet=self.wallet, category=self.category, amount=10, date=dt.date(2026, 9, 1)
        )
        with mock.patch("django.core.files.storage.FileSystemStorage.delete") as borrar:
            txn.delete()
        borrar.assert_not_called()

    def test_si_el_storage_falla_el_borrado_igual_ocurre(self):
        # Un blob que no se pudo borrar es un centavo; un borrado que revienta
        # (y revierte la transacción de base) le rompe el día al usuario.
        txn, _ = self._con_recibo()
        with mock.patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("GCS caído"),
        ):
            with self.assertLogs("apps.transactions.signals", level="WARNING"):
                txn.delete()
        self.assertFalse(Transaction.all_objects.filter(pk=txn.pk).exists())
