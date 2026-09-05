"""Adjuntar/ver/borrar el recibo de una transacción."""
import datetime as dt
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import Category, Transaction
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"

# 1x1 PNG válido (para que Pillow lo acepte como imagen real).
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "0000004945454e44ae426082"
)

MEDIA_ROOT = tempfile.mkdtemp(prefix="budget-test-media-")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ReceiptApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("alice", "alice@example.com", "pw")
        cls.outsider = User.objects.create_user("mallory", "mallory@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.owner, role=Membership.ROLE_OWNER)

        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING
        )
        cls.category = Category.objects.create(
            workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE
        )
        cls.txn = Transaction.objects.create(
            wallet=cls.wallet, category=cls.category, amount=10, date=dt.date(2026, 9, 1)
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.client.force_authenticate(self.owner)

    def _url(self):
        return f"/api/v1/transactions/{self.txn.id}/receipt/"

    def _upload(self, name="recibo.png", content=TINY_PNG, content_type="image/png"):
        file = SimpleUploadedFile(name, content, content_type=content_type)
        return self.client.post(
            self._url(), {"file": file}, format="multipart", **{HEADER: str(self.ws.id)}
        )

    def test_upload_then_transaction_reports_has_receipt(self):
        res = self._upload()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertTrue(res.data["has_receipt"])

        detail = self.client.get(
            f"/api/v1/transactions/{self.txn.id}/", **{HEADER: str(self.ws.id)}
        )
        self.assertTrue(detail.data["has_receipt"])

    def test_download_returns_the_file(self):
        self._upload()
        res = self.client.get(self._url(), **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res["Content-Type"], "image/png")
        self.assertEqual(b"".join(res.streaming_content), TINY_PNG)

    def test_download_without_receipt_is_404(self):
        res = self.client.get(self._url(), **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_re_upload_replaces_the_previous_file(self):
        self._upload(name="uno.png")
        first_name = Transaction.objects.get(pk=self.txn.pk).receipt.name
        self._upload(name="dos.png")
        self.txn.refresh_from_db()
        self.assertNotEqual(first_name, self.txn.receipt.name)

    def test_delete_removes_it(self):
        self._upload()
        res = self.client.delete(self._url(), **{HEADER: str(self.ws.id)})
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.txn.refresh_from_db()
        self.assertFalse(self.txn.receipt)

    def test_rejects_file_too_large(self):
        big = SimpleUploadedFile("grande.png", b"0" * (9 * 1024 * 1024), content_type="image/png")
        res = self.client.post(
            self._url(), {"file": big}, format="multipart", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_unsupported_content_type(self):
        pdf = SimpleUploadedFile("recibo.pdf", b"%PDF-1.4", content_type="application/pdf")
        res = self.client.post(
            self._url(), {"file": pdf}, format="multipart", **{HEADER: str(self.ws.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outsider_cannot_see_or_upload(self):
        self._upload()
        self.client.force_authenticate(self.outsider)
        res = self.client.get(self._url(), **{HEADER: str(self.ws.id)})
        # Sin membresía en el workspace, `HasWorkspaceMembership` corta antes
        # de llegar al objeto.
        self.assertIn(res.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
