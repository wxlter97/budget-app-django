"""GET/POST /api/v1/workspaces/{id}/backup/ y /restore/ -- exportar todo el
workspace a JSON y poder restaurarlo (mejora sugerida en la hoja de ruta)."""
import datetime as dt
import time
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Wallet
from apps.transactions.models import (
    Category,
    CategoryBudget,
    InstallmentPurchase,
    RecurringExpense,
    Tag,
    Transaction,
)
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import export_backup, import_backup

User = get_user_model()


class BackupExportTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.member = User.objects.create_user("member", "m@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa", base_currency="USD")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)

        self.group = Category.objects.create(workspace=self.ws, name="Casa", type=Category.TYPE_EXPENSE)
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE, parent=self.group
        )
        self.salary = Category.objects.create(workspace=self.ws, name="Sueldo", type=Category.TYPE_INCOME)

        self.checking = Wallet.objects.create(
            workspace=self.ws, name="Banco", opening_balance=Decimal("1000.00"),
        )
        self.savings = Wallet.objects.create(
            workspace=self.ws, name="Ahorro", parent=self.checking, purpose=Wallet.PURPOSE_SAVINGS,
            goal_amount=Decimal("500.00"),
        )
        self.tag = Tag.objects.create(workspace=self.ws, name="viaje")

        self.txn = Transaction.objects.create(
            wallet=self.checking, category=self.food, amount=Decimal("40.00"),
            date=dt.date(2026, 9, 1), created_by=self.owner,
        )
        self.txn.tags.set([self.tag])

        CategoryBudget.objects.create(
            workspace=self.ws, category=self.food, amount=Decimal("300"), month=9, year=2026,
        )
        RecurringExpense.objects.create(
            workspace=self.ws, category=self.food, wallet=self.checking,
            amount=Decimal("15.00"), next_due_date=dt.date(2026, 10, 1),
        )
        InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=self.checking, category=self.food,
            description="Sofá", total_amount=Decimal("300.00"), installment_amount=Decimal("100.00"),
            installments_total=3, start_date=dt.date(2026, 8, 1),
        )

    def _url(self, action="backup"):
        return f"/api/v1/workspaces/{self.ws.id}/{action}/"

    def test_only_owner_can_export(self):
        self.client.force_authenticate(self.member)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_export_shape(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertEqual(data["format"], "budget-app-backup")
        self.assertEqual(data["workspace_name"], "Casa")
        self.assertEqual(len(data["wallets"]), 2)
        self.assertEqual(len(data["categories"]), 3)
        self.assertEqual(len(data["tags"]), 1)
        self.assertEqual(len(data["transactions"]), 1)
        self.assertEqual(len(data["category_budgets"]), 1)
        self.assertEqual(len(data["recurring_expenses"]), 1)
        self.assertEqual(len(data["installment_purchases"]), 1)

        txn_row = data["transactions"][0]
        self.assertEqual(txn_row["id"], str(self.txn.id))
        self.assertEqual(txn_row["wallet"], str(self.checking.id))
        self.assertEqual(txn_row["tags"], [str(self.tag.id)])
        self.assertEqual(txn_row["created_by_username"], "owner")

        savings_row = next(w for w in data["wallets"] if w["id"] == str(self.savings.id))
        self.assertEqual(savings_row["parent"], str(self.checking.id))

        food_row = next(c for c in data["categories"] if c["id"] == str(self.food.id))
        self.assertEqual(food_row["parent"], str(self.group.id))


class BackupRestoreServiceTests(APITestCase):
    """Prueba `import_backup` directamente (round-trip completo)."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)

        self.parent_wallet = Wallet.objects.create(
            workspace=self.ws, name="Banco", opening_balance=Decimal("500.00"), is_default=True,
        )
        self.child_wallet = Wallet.objects.create(
            workspace=self.ws, name="Sobre", parent=self.parent_wallet,
        )
        self.group = Category.objects.create(workspace=self.ws, name="Casa", type=Category.TYPE_EXPENSE)
        self.food = Category.objects.create(
            workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE, parent=self.group
        )
        self.tag = Tag.objects.create(workspace=self.ws, name="viaje")
        self.txn1 = Transaction.objects.create(
            wallet=self.parent_wallet, category=self.food, amount=Decimal("40.00"),
            date=dt.date(2026, 9, 1), created_by=self.owner,
        )
        self.txn1.tags.set([self.tag])
        Transaction.objects.create(
            wallet=self.parent_wallet, category=self.food, amount=Decimal("10.00"),
            date=dt.date(2026, 9, 2), created_by=self.owner,
        )
        self.parent_wallet.refresh_from_db()

    def test_round_trip_recreates_everything_and_rebuilds_balances(self):
        backup = export_backup(self.ws)
        original_balance = self.parent_wallet.current_balance
        # `Model.delete()` pisa `.pk` a None en la instancia -- se guardan los
        # IDs originales antes de borrar para poder buscarlos después.
        parent_id, child_id = self.parent_wallet.id, self.child_wallet.id
        food_id, group_id, tag_id, txn1_id = self.food.id, self.group.id, self.tag.id, self.txn1.id

        # Simula "algo salió mal": se borra todo el workspace a mano
        # (respetando FKs protegidas: transacciones antes que categorías,
        # cartera hija antes que la padre).
        Transaction.all_objects.filter(wallet__workspace=self.ws).delete()
        self.child_wallet.delete()
        self.parent_wallet.delete()
        self.food.delete()
        self.group.delete()

        summary = import_backup(self.ws, backup, self.owner)
        self.assertEqual(summary["wallets"], 2)
        self.assertEqual(summary["transactions"], 2)

        restored_parent = Wallet.objects.get(id=parent_id)
        restored_child = Wallet.objects.get(id=child_id)
        self.assertEqual(restored_child.parent_id, restored_parent.id)
        self.assertTrue(restored_parent.is_default)
        # El saldo se reconstruye solo, transacción a transacción -- no se
        # copia del backup.
        self.assertEqual(restored_parent.current_balance, original_balance)

        restored_food = Category.objects.get(id=food_id)
        self.assertEqual(restored_food.parent_id, group_id)

        restored_txn = Transaction.objects.get(id=txn1_id)
        self.assertEqual([t.id for t in restored_txn.tags.all()], [tag_id])
        self.assertEqual(restored_txn.created_by_id, self.owner.id)

    def test_wipes_existing_data_before_restoring(self):
        backup = export_backup(self.ws)
        # Se agrega basura nueva que NO está en el backup.
        junk = Wallet.objects.create(workspace=self.ws, name="Basura")

        import_backup(self.ws, backup, self.owner)

        self.assertFalse(Wallet.objects.filter(id=junk.id).exists())
        self.assertEqual(Wallet.objects.filter(workspace=self.ws).count(), 2)

    def test_rejects_invalid_format(self):
        from apps.workspaces.services import BackupError

        with self.assertRaises(BackupError):
            import_backup(self.ws, {"format": "otra-cosa"}, self.owner)

    def test_falls_back_to_requesting_user_when_original_creator_is_gone(self):
        from apps.workspaces.services import wipe_workspace_data

        backup = export_backup(self.ws)
        # El workspace original ya no existe (p. ej. se restauró en otra
        # cuenta) -- si no, sus filas seguirían usando los mismos UUIDs que
        # el backup y la restauración chocaría con ellas.
        wipe_workspace_data(self.ws, scope="todo")

        other_ws = Workspace.objects.create(name="Otra")
        stranger = User.objects.create_user("stranger", "s@e.com", "pw")
        Membership.objects.create(workspace=other_ws, user=stranger, role=Membership.ROLE_OWNER)

        import_backup(other_ws, backup, stranger)
        restored = Transaction.objects.filter(wallet__workspace=other_ws).first()
        self.assertEqual(restored.created_by_id, stranger.id)

    def test_rejects_restoring_into_another_workspace_while_original_still_has_the_data(self):
        """Los UUID del backup son globales por tabla: restaurarlo en un
        presupuesto distinto mientras el original sigue existiendo chocaría
        -- se rechaza con un error claro en vez de un IntegrityError crudo."""
        from apps.workspaces.services import BackupError

        backup = export_backup(self.ws)
        other_ws = Workspace.objects.create(name="Otra")
        Membership.objects.create(workspace=other_ws, user=self.owner, role=Membership.ROLE_OWNER)

        with self.assertRaises(BackupError):
            import_backup(other_ws, backup, self.owner)
        # Y no tocó nada del workspace destino al rechazarlo.
        self.assertEqual(Wallet.objects.filter(workspace=other_ws).count(), 0)

    def test_accepts_explicit_nulls_for_fields_with_a_sensible_default(self):
        """Un respaldo armado a mano (migrando desde otra app) fácilmente manda
        `null` donde nuestro propio `export_backup` nunca lo haría -- p. ej.
        `"description": null` en vez de simplemente omitir la clave. No debe
        reventar con un IntegrityError crudo: tiene que caer al mismo default
        que si la clave no viniera (justo lo que pasó con un respaldo real de
        una migración desde otra app: 500 al restaurar)."""
        wallet_id = str(uuid.uuid4())
        cat_id = str(uuid.uuid4())
        backup = {
            "format": "budget-app-backup",
            "version": 1,
            "workspace_name": "Casa",
            "base_currency": "USD",
            "wallets": [
                {
                    "id": wallet_id, "name": "Banco", "purpose": None, "kind": None,
                    "currency": None, "color": None, "opening_balance": None,
                    "counts_toward_net_worth": None, "counterparty": None, "visibility": None,
                    "is_active": None, "is_archived": None, "sort_order": None,
                    "is_default": None, "parent": None, "owner_username": None,
                }
            ],
            "categories": [
                {
                    "id": cat_id, "name": "Varios", "icon": None, "color": None,
                    "type": "expense", "sort_order": None, "parent": None,
                }
            ],
            "tags": [],
            "category_budgets": [],
            "recurring_expenses": [],
            "installment_purchases": [],
            "transactions": [
                {
                    "id": str(uuid.uuid4()), "type": "expense", "wallet": wallet_id, "to_wallet": None,
                    "category": cat_id, "amount": "10.00", "description": None, "date": "2026-09-01",
                    "counts_toward_budget": None, "source": None, "is_recurring": None,
                    "split_group": None, "created_by_username": None, "tags": None,
                }
            ],
        }

        summary = import_backup(self.ws, backup, self.owner)
        self.assertEqual(summary["transactions"], 1)

        wallet = Wallet.objects.get(id=wallet_id)
        self.assertEqual(wallet.purpose, Wallet.PURPOSE_SPENDING)
        self.assertEqual(wallet.currency, "USD")
        self.assertTrue(wallet.is_active)

        txn = Transaction.objects.get(wallet_id=wallet_id)
        self.assertEqual(txn.description, "")
        self.assertTrue(txn.counts_toward_budget)
        self.assertEqual(txn.source, Transaction.SOURCE_MANUAL)

    def test_rejects_backup_missing_a_required_field(self):
        """Falta (o viene en `null`) un campo sin default sensato -- acá, el
        `type` de una transacción -- se rechaza con BackupError y sin tocar
        nada, no con un IntegrityError crudo a mitad de la restauración."""
        from apps.workspaces.services import BackupError

        wallet_id = str(uuid.uuid4())
        cat_id = str(uuid.uuid4())
        backup = {
            "format": "budget-app-backup",
            "version": 1,
            "workspace_name": "Casa",
            "base_currency": "USD",
            "wallets": [{"id": wallet_id, "name": "Banco"}],
            "categories": [{"id": cat_id, "name": "Varios", "type": "expense"}],
            "tags": [],
            "category_budgets": [],
            "recurring_expenses": [],
            "installment_purchases": [],
            "transactions": [
                {"id": str(uuid.uuid4()), "type": None, "wallet": wallet_id, "amount": "10.00", "date": "2026-09-01"}
            ],
        }

        with self.assertRaises(BackupError):
            import_backup(self.ws, backup, self.owner)
        # No tocó nada: los datos originales del workspace siguen ahí.
        self.assertTrue(Wallet.objects.filter(id=self.parent_wallet.id).exists())


class BackupRestoreApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.member = User.objects.create_user("member", "m@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        Membership.objects.create(workspace=self.ws, user=self.member, role=Membership.ROLE_MEMBER)
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Banco")
        self.cat = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)
        Transaction.objects.create(
            wallet=self.wallet, category=self.cat, amount=Decimal("20.00"), date=dt.date(2026, 9, 1),
        )

    def test_requires_confirm(self):
        self.client.force_authenticate(self.owner)
        backup = self.client.get(f"/api/v1/workspaces/{self.ws.id}/backup/").data
        resp = self.client.post(f"/api/v1/workspaces/{self.ws.id}/restore/", backup, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_member_cannot_restore(self):
        self.client.force_authenticate(self.member)
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws.id}/restore/", {"confirm": True}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_backup_then_restore_round_trip_via_api(self):
        self.client.force_authenticate(self.owner)
        backup = self.client.get(f"/api/v1/workspaces/{self.ws.id}/backup/").data
        backup["confirm"] = True
        resp = self.client.post(f"/api/v1/workspaces/{self.ws.id}/restore/", backup, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["restored"]["transactions"], 1)
        self.assertEqual(Transaction.objects.filter(wallet__workspace=self.ws).count(), 1)

    def test_rejects_malformed_backup(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws.id}/restore/",
            {"confirm": True, "format": "nope"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class BackupRestoreScaleTests(APITestCase):
    """Un respaldo grande (miles de movimientos, como el de alguien migrando
    desde otra app) no debe restaurarse fila por fila: eso fue justo lo que
    hacía que un restore tardara tanto que el request llegaba a hacer
    timeout. `import_backup` tiene que usar una cantidad de consultas que
    no crezca con la cantidad de movimientos."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@e.com", "pw")
        self.ws = Workspace.objects.create(name="Casa")
        Membership.objects.create(workspace=self.ws, user=self.owner, role=Membership.ROLE_OWNER)
        self.wallet = Wallet.objects.create(workspace=self.ws, name="Banco")
        self.food = Category.objects.create(workspace=self.ws, name="Comida", type=Category.TYPE_EXPENSE)
        self.salary = Category.objects.create(workspace=self.ws, name="Sueldo", type=Category.TYPE_INCOME)

    def _big_backup(self, n):
        txns = []
        for i in range(n):
            is_income = i % 10 == 0
            txns.append(
                {
                    "id": str(uuid.uuid4()),
                    "type": "income" if is_income else "expense",
                    "wallet": str(self.wallet.id),
                    "to_wallet": None,
                    "category": str(self.salary.id if is_income else self.food.id),
                    "amount": "100.00" if is_income else "10.00",
                    "description": f"mov {i}",
                    "date": "2026-01-01",
                    "counts_toward_budget": True,
                    "source": "manual",
                    "is_recurring": False,
                    "split_group": None,
                    "created_by_username": "owner",
                    "tags": [],
                }
            )
        return {
            "format": "budget-app-backup",
            "version": 1,
            "exported_at": "2026-09-05T20:58:00Z",
            "workspace_name": "Casa",
            "base_currency": "USD",
            "wallets": [
                {
                    "id": str(self.wallet.id),
                    "name": "Banco",
                    "purpose": "spending",
                    "kind": "bank",
                    "currency": "USD",
                    "color": "",
                    "opening_balance": "0",
                    "counts_toward_net_worth": True,
                    "goal_amount": None,
                    "goal_date": None,
                    "monthly_contribution": None,
                    "credit_limit": None,
                    "card_last4": "",
                    "billing_cycle_day": None,
                    "payment_due_day": None,
                    "interest_rate": None,
                    "due_date": None,
                    "counterparty": "",
                    "visibility": "shared",
                    "owner_username": None,
                    "is_active": True,
                    "is_archived": False,
                    "sort_order": 0,
                    "is_default": True,
                    "parent": None,
                }
            ],
            "categories": [
                {
                    "id": str(self.food.id), "name": "Comida", "icon": "", "color": "",
                    "type": "expense", "sort_order": 0, "parent": None,
                },
                {
                    "id": str(self.salary.id), "name": "Sueldo", "icon": "", "color": "",
                    "type": "income", "sort_order": 0, "parent": None,
                },
            ],
            "tags": [],
            "category_budgets": [],
            "recurring_expenses": [],
            "installment_purchases": [],
            "transactions": txns,
        }

    def test_restores_thousands_of_transactions_quickly_and_in_few_queries(self):
        n = 3000
        backup = self._big_backup(n)

        start = time.monotonic()
        with CaptureQueriesContext(connection) as ctx:
            summary = import_backup(self.ws, backup, self.owner)
        elapsed = time.monotonic() - start

        self.assertEqual(summary["transactions"], n)
        # Lo que importa no es el número exacto sino que no escale con `n`:
        # muy por debajo de una consulta por movimiento.
        self.assertLess(len(ctx.captured_queries), 100)
        self.assertLess(elapsed, 10, "restaurar 3000 movimientos no debería tardar tanto")

        self.assertEqual(Transaction.objects.filter(wallet__workspace=self.ws).count(), n)
        self.wallet.refresh_from_db()
        # 300 ingresos de 100 + 2700 gastos de 10 = 30000 - 27000
        self.assertEqual(self.wallet.current_balance, Decimal("3000.00"))
