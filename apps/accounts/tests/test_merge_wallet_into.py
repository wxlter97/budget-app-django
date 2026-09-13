"""manage.py merge_wallet_into: fusionar una cartera vacía (típicamente una
tarjeta adicional creada por error como cartera aparte) en la que debía
ser desde el principio."""
import datetime as dt
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.accounts.models import Wallet, WalletCard
from apps.transactions.models import Category, InstallmentPurchase, RecurringExpense, Transaction
from apps.workspaces.models import Workspace


class MergeWalletIntoTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="W")
        self.titular = Wallet.objects.create(
            workspace=self.ws, name="Cuscatlán titular", kind=Wallet.KIND_CREDIT,
            card_last4="8303", credit_limit=Decimal("2000.00"),
        )
        self.adicional = Wallet.objects.create(
            workspace=self.ws, name="Cuscatlán adicional", kind=Wallet.KIND_CREDIT,
            card_last4="9126", parent=self.titular,
        )

    def _run(self, origen, destino, **opts):
        out = StringIO()
        call_command(
            "merge_wallet_into", str(origen.id), str(destino.id),
            workspace=str(self.ws.id), stdout=out, **opts,
        )
        return out.getvalue()

    def test_merges_card_last4_as_extra_card_of_destino(self):
        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        self.assertEqual(self.titular.card_last4, "8303")  # no se pisa
        extra = list(self.titular.extra_cards.all())
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0].last4, "9126")
        self.assertEqual(extra[0].label, "Cuscatlán adicional")

    def test_origen_gets_soft_deleted(self):
        self._run(self.adicional, self.titular)
        self.assertFalse(Wallet.objects.filter(id=self.adicional.id).exists())
        self.assertTrue(Wallet.all_objects.get(id=self.adicional.id).is_deleted)

    def test_promotes_last4_when_destino_has_none(self):
        self.titular.card_last4 = ""
        self.titular.save(update_fields=["card_last4"])

        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        self.assertEqual(self.titular.card_last4, "9126")
        self.assertEqual(self.titular.extra_cards.count(), 0)  # no queda duplicada

    def test_moves_origens_own_extra_cards_too(self):
        WalletCard.objects.create(wallet=self.adicional, last4="0001", label="Otra")
        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        last4s = set(self.titular.extra_cards.values_list("last4", flat=True))
        self.assertEqual(last4s, {"9126", "0001"})

    def test_skips_last4_already_present_on_destino(self):
        WalletCard.objects.create(wallet=self.titular, last4="9126")  # ya estaba
        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        self.assertEqual(self.titular.extra_cards.count(), 1)  # no se duplicó

    def test_refuses_when_origen_has_transactions(self):
        cat = Category.objects.create(workspace=self.ws, name="Compras", type=Category.TYPE_EXPENSE)
        Transaction.objects.create(
            wallet=self.adicional, category=cat, amount=Decimal("10.00"), date=dt.date(2026, 1, 1)
        )
        with self.assertRaises(CommandError):
            self._run(self.adicional, self.titular)
        self.assertTrue(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_refuses_when_origen_has_children(self):
        Wallet.objects.create(workspace=self.ws, name="Nieta", parent=self.adicional)
        with self.assertRaises(CommandError):
            self._run(self.adicional, self.titular)

    def test_refuses_same_wallet(self):
        with self.assertRaises(CommandError):
            self._run(self.titular, self.titular)

    def test_lookup_by_name(self):
        call_command(
            "merge_wallet_into", "Cuscatlán adicional", "Cuscatlán titular",
            workspace=self.ws.name, stdout=StringIO(),
        )
        self.assertFalse(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_ambiguous_name_requires_uuid(self):
        Wallet.objects.create(workspace=self.ws, name="Cuscatlán titular", kind=Wallet.KIND_CREDIT)
        with self.assertRaises(CommandError):
            call_command(
                "merge_wallet_into", str(self.adicional.id), "Cuscatlán titular",
                workspace=str(self.ws.id), stdout=StringIO(),
            )

    def test_workspace_auto_detected_when_only_one(self):
        call_command(
            "merge_wallet_into", str(self.adicional.id), str(self.titular.id), stdout=StringIO()
        )
        self.assertFalse(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_move_activity_reassigns_transactions_recurring_installments_and_children(self):
        cat = Category.objects.create(workspace=self.ws, name="Compras", type=Category.TYPE_EXPENSE)
        tx = Transaction.objects.create(
            wallet=self.adicional, category=cat, amount=Decimal("10.00"), date=dt.date(2026, 1, 1)
        )
        rec = RecurringExpense.objects.create(
            workspace=self.ws, wallet=self.adicional, category=cat, name="Netflix",
            amount=Decimal("9.99"), next_due_date=dt.date(2026, 2, 5),
        )
        inst = InstallmentPurchase.objects.create(
            workspace=self.ws, wallet=self.adicional, category=cat, description="TV",
            total_amount=Decimal("300.00"), installments_total=6, start_date=dt.date(2026, 1, 1),
        )
        nieta = Wallet.objects.create(workspace=self.ws, name="Nieta", parent=self.adicional)

        self._run(self.adicional, self.titular, move_activity=True)

        tx.refresh_from_db()
        rec.refresh_from_db()
        inst.refresh_from_db()
        nieta.refresh_from_db()
        self.assertEqual(tx.wallet_id, self.titular.id)
        self.assertEqual(rec.wallet_id, self.titular.id)
        self.assertEqual(inst.wallet_id, self.titular.id)
        self.assertEqual(nieta.parent_id, self.titular.id)
        self.assertFalse(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_move_activity_dry_run_does_not_write(self):
        cat = Category.objects.create(workspace=self.ws, name="Compras", type=Category.TYPE_EXPENSE)
        tx = Transaction.objects.create(
            wallet=self.adicional, category=cat, amount=Decimal("10.00"), date=dt.date(2026, 1, 1)
        )

        self._run(self.adicional, self.titular, move_activity=True, dry_run=True)

        tx.refresh_from_db()
        self.assertEqual(tx.wallet_id, self.adicional.id)
        self.assertTrue(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_move_activity_still_refuses_crossed_transaction(self):
        cat = Category.objects.create(workspace=self.ws, name="Pagos", type=Category.TYPE_EXPENSE)
        Transaction.objects.create(
            wallet=self.adicional, to_wallet=self.titular, category=cat,
            amount=Decimal("50.00"), date=dt.date(2026, 1, 1),
        )
        with self.assertRaises(CommandError):
            self._run(self.adicional, self.titular, move_activity=True)
        self.assertTrue(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_without_move_activity_flag_still_refuses(self):
        cat = Category.objects.create(workspace=self.ws, name="Compras", type=Category.TYPE_EXPENSE)
        Transaction.objects.create(
            wallet=self.adicional, category=cat, amount=Decimal("10.00"), date=dt.date(2026, 1, 1)
        )
        with self.assertRaises(CommandError):
            self._run(self.adicional, self.titular)
        self.assertTrue(Wallet.objects.filter(id=self.adicional.id).exists())

    def test_destino_that_is_actually_origens_child_gets_reparented_to_none(self):
        # Caso real: "origen" es el contenedor vacío que dejó "Dividir
        # cartera" y "destino" es la hija que se quedó con toda la
        # actividad -- al revés de lo que el nombre origen/destino sugiere.
        self.adicional.parent = None
        self.adicional.save(update_fields=["parent"])
        self.titular.parent = self.adicional
        self.titular.save(update_fields=["parent"])

        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        self.assertIsNone(self.titular.parent_id)

    def test_destino_that_is_origens_child_gets_promoted_to_origens_parent(self):
        abuela = Wallet.objects.create(workspace=self.ws, name="Abuela")
        self.adicional.parent = abuela
        self.adicional.save(update_fields=["parent"])
        self.titular.parent = self.adicional
        self.titular.save(update_fields=["parent"])

        self._run(self.adicional, self.titular)

        self.titular.refresh_from_db()
        self.assertEqual(self.titular.parent_id, abuela.id)

    def test_workspace_required_when_more_than_one(self):
        Workspace.objects.create(name="Otro")
        with self.assertRaises(CommandError):
            call_command(
                "merge_wallet_into", str(self.adicional.id), str(self.titular.id), stdout=StringIO()
            )
