"""Un GET no escribe: por eso `AtomicOnlyForWritesMixin` puede quitarles la transacción.

Sin `BEGIN`/`COMMIT` una lectura ahorra dos viajes a Neon, pero si un GET escribiera
varias filas podría dejarlas a medias. Este test recorre los listados y las acciones
GET de los viewsets que usan el mixin y falla si alguno emite un INSERT/UPDATE/DELETE.
"""
import datetime as dt
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase, APITransactionTestCase

from apps.accounts.models import Wallet
from apps.common.api import AtomicOnlyForWritesMixin, WorkspaceScopedViewSet
from apps.transactions.models import Category, Tag, Transaction
from apps.workspaces.models import Membership, Workspace
from config.api_router import router

User = get_user_model()
HEADER = "HTTP_X_WORKSPACE_ID"
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


class GetsDoNotWriteTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice", "a@example.com", "pw")
        cls.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=cls.ws, user=cls.user, role=Membership.ROLE_OWNER)
        cls.wallet = Wallet.objects.create(
            workspace=cls.ws, name="Tarjeta", purpose=Wallet.PURPOSE_SPENDING, billing_cycle_day=5
        )
        cls.food = Category.objects.create(workspace=cls.ws, name="Comida", type=Category.TYPE_EXPENSE)
        cls.tag = Tag.objects.create(workspace=cls.ws, name="viaje")
        today = dt.date.today()
        for i in range(8):
            t = Transaction.objects.create(
                wallet=cls.wallet, category=cls.food, amount=Decimal("15.00"),
                date=today - dt.timedelta(days=30 * i), description="Netflix",
            )
            t.tags.add(cls.tag)

    def setUp(self):
        self.client.force_authenticate(self.user)

    def _get_urls(self):
        """Listado de cada viewset con el mixin y sus acciones GET sin id."""
        wid = str(self.wallet.id)
        urls = []
        for prefix, viewset, _ in router.registry:
            if not issubclass(viewset, AtomicOnlyForWritesMixin):
                continue
            urls.append(f"/api/v1/{prefix}/")
            for action in viewset.get_extra_actions():
                if "get" in action.mapping and not action.detail:
                    urls.append(f"/api/v1/{prefix}/{action.url_path}/")
        # Acciones de detalle sobre una cartera real.
        urls += [f"/api/v1/wallets/{wid}/{p}/" for p in ("statement", "projection", "interest-projection")]
        urls.append(f"/api/v1/wallets/{wid}/")
        # Vistas sueltas y viewsets fuera de `WorkspaceScopedViewSet` que también usan el mixin.
        urls += [
            "/api/v1/workspaces/", "/api/v1/notifications/", "/api/v1/notifications/unread-count/",
            "/api/v1/module-flags/", "/api/v1/ai/status/", "/api/v1/billing/me/",
            "/api/v1/reports/net-worth/", "/api/v1/reports/summary/", "/api/v1/reports/budget/",
            "/api/v1/reports/cashflow/", "/api/v1/reports/scheduled/", "/api/v1/reports/category-trends/",
        ]
        return urls

    def test_no_get_writes(self):
        urls = self._get_urls()
        self.assertGreater(len(urls), 8)  # que el recorrido no quede vacío por un cambio del router
        for url in urls:
            with self.subTest(url=url), CaptureQueriesContext(connection) as ctx:
                res = self.client.get(url, **{HEADER: str(self.ws.id)})
                self.assertLess(res.status_code, 500, url)
            writes = [q["sql"][:120] for q in ctx.captured_queries if WRITE.match(q["sql"])]
            self.assertEqual(writes, [], f"GET {url} escribió")

    def test_the_workspace_viewsets_use_the_mixin(self):
        self.assertTrue(issubclass(WorkspaceScopedViewSet, AtomicOnlyForWritesMixin))


class TransactionsAroundRequestsTests(APITransactionTestCase):
    """Sin la transacción envolvente de `TestCase`, como en producción."""

    def setUp(self):
        self.user = User.objects.create_user("bob", "b@example.com", "pw")
        self.ws = Workspace.objects.create(name="B")
        Membership.objects.create(workspace=self.ws, user=self.user, role=Membership.ROLE_OWNER)
        self.client.force_authenticate(self.user)

    def _statements(self, method, url, data=None, **kw):
        with CaptureQueriesContext(connection) as ctx:
            res = getattr(self.client, method)(url, data, **{HEADER: str(self.ws.id)}, **kw)
        return res, [q["sql"].strip().upper() for q in ctx.captured_queries]

    def test_a_get_runs_without_begin_and_commit(self):
        res, sql = self._statements("get", "/api/v1/categories/")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("BEGIN", sql)
        self.assertNotIn("COMMIT", sql)

    def test_a_write_still_runs_inside_a_transaction(self):
        res, sql = self._statements(
            "post", "/api/v1/categories/", {"name": "Comida", "type": "expense"}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        self.assertIn("BEGIN", sql)
        self.assertIn("COMMIT", sql)

    def test_a_write_that_fails_after_writing_is_rolled_back(self):
        # `split` crea partes y falla si no suman: nada debe quedar guardado.
        wallet = Wallet.objects.create(workspace=self.ws, name="C", purpose=Wallet.PURPOSE_SPENDING)
        food = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)
        txn = Transaction.objects.create(
            wallet=wallet, category=food, amount=Decimal("100"), date=dt.date(2026, 8, 1)
        )
        before = Transaction.objects.count()
        res = self.client.post(
            f"/api/v1/transactions/{txn.id}/split/",
            {"parts": [{"category": str(food.id), "amount": "10"}, {"category": str(food.id), "amount": "10"}]},
            format="json", **{HEADER: str(self.ws.id)},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Transaction.objects.count(), before)
