# Generated manually -- backfill de datos, no de esquema.
from django.db import migrations


def backfill_kind(apps, schema_editor):
    """`kind` se agregó en 0007 con `default="bank"` para TODAS las wallets
    ya existentes, sin backfill -- para entonces el viejo campo `type`
    (checking/savings/credit/cash, ver 0001/0004) ya estaba borrado, así que
    su valor original no es recuperable. Esto es lo mejor que se puede
    inferir de los campos que sí sobrevivieron: mismo criterio de
    "¿es tarjeta de crédito?" que ya usa el resto del sistema (ver
    `apps.accounts.services.installment_status` y validaciones de
    InstallmentPurchase, que exigen `kind=credit` + `billing_cycle_day`).
    Sólo toca wallets que quedaron en el `kind="bank"` por defecto -- si
    alguien ya corrigió el subtipo a mano desde entonces, no se pisa."""
    Wallet = apps.get_model("accounts", "Wallet")
    for wallet in Wallet.objects.filter(kind="bank"):
        if wallet.purpose == "debt":
            has_card_signal = bool(wallet.card_last4) or wallet.billing_cycle_day is not None
            wallet.kind = "credit" if has_card_signal else "custom"
            wallet.save(update_fields=["kind"])
        elif wallet.purpose == "asset":
            wallet.kind = "custom"
            wallet.save(update_fields=["kind"])


def noop_reverse(apps, schema_editor):
    # No hay vuelta atrás posible: el dato original ya se había perdido antes
    # de esta migración, así que "revertir" sólo podría reponer el default
    # `bank` que ya traían -- no hace falta una operación explícita para eso.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_walletcard"),
    ]

    operations = [
        migrations.RunPython(backfill_kind, noop_reverse),
    ]
