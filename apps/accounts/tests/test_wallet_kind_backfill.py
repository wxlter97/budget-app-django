"""`kind` se agregó (migración 0007) con default="bank" para TODAS las
wallets ya existentes, sin backfill -- ver migración de datos 0014. Estos
tests corren esa misma función de backfill directamente (no la corrida
completa de migraciones, que hoy no se ejercita en la test suite) sobre el
estado actual del modelo, que es idéntico al que tenía justo después de
aplicarla."""
import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from apps.accounts.models import Wallet
from apps.workspaces.models import Workspace

_migration = importlib.import_module("apps.accounts.migrations.0014_backfill_wallet_kind")


class WalletKindBackfillTests(TestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Casa")

    def _run_backfill(self):
        _migration.backfill_kind(global_apps, None)

    def test_debt_wallet_with_card_last4_becomes_credit(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta vieja", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_BANK, card_last4="1234",
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_CREDIT)

    def test_debt_wallet_with_billing_cycle_day_becomes_credit(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Tarjeta vieja 2", purpose=Wallet.PURPOSE_DEBT,
            kind=Wallet.KIND_BANK, billing_cycle_day=15,
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_CREDIT)

    def test_debt_wallet_without_card_signal_becomes_custom(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Préstamo", purpose=Wallet.PURPOSE_DEBT, kind=Wallet.KIND_BANK,
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_CUSTOM)

    def test_asset_wallet_becomes_custom(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Carro", purpose=Wallet.PURPOSE_ASSET, kind=Wallet.KIND_BANK,
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_CUSTOM)

    def test_spending_wallet_left_as_bank(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Banco", purpose=Wallet.PURPOSE_SPENDING, kind=Wallet.KIND_BANK,
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_BANK)

    def test_wallet_already_migrated_away_from_default_is_not_overwritten(self):
        wallet = Wallet.objects.create(
            workspace=self.ws, name="Efectivo", purpose=Wallet.PURPOSE_SPENDING, kind=Wallet.KIND_CASH,
        )
        self._run_backfill()
        wallet.refresh_from_db()
        self.assertEqual(wallet.kind, Wallet.KIND_CASH)
