"""GET/POST /api/v1/workspaces/{id}/backup/ y /restore/ -- exportar todo el
workspace a JSON y poder restaurarlo (mejora sugerida en la hoja de ruta)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
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
