"""Fusiona Asset / Liability / Debt en Wallet y borra esos modelos."""
from decimal import Decimal

from django.db import migrations


def absorb(apps, schema_editor):
    Wallet = apps.get_model("accounts", "Wallet")
    Asset = apps.get_model("accounts", "Asset")
    Liability = apps.get_model("accounts", "Liability")
    Debt = apps.get_model("accounts", "Debt")

    for a in Asset.objects.all():
        Wallet.objects.create(
            workspace_id=a.workspace_id,
            name=a.name,
            purpose="asset",
            opening_balance=a.current_value,
            current_balance=a.current_value,
            visibility=a.visibility,
            owner_id=a.owner_id,
            counts_toward_net_worth=True,
            is_deleted=a.is_deleted,
        )

    for lb in Liability.objects.all():
        Wallet.objects.create(
            workspace_id=lb.workspace_id,
            name=lb.name,
            purpose="debt",
            opening_balance=-lb.remaining_amount,
            current_balance=-lb.remaining_amount,
            interest_rate=lb.interest_rate,
            due_date=lb.due_date,
            counts_toward_net_worth=True,
            is_deleted=lb.is_deleted,
        )

    for d in Debt.objects.all():
        signed = d.amount if d.direction == "a_favor" else -d.amount
        Wallet.objects.create(
            workspace_id=d.workspace_id,
            name=d.person,
            purpose="debt",
            counterparty=d.person,
            opening_balance=signed,
            current_balance=signed,
            counts_toward_net_worth=True,
            is_active=not d.is_settled,
            is_deleted=d.is_deleted,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_wallet"),
    ]

    operations = [
        migrations.RunPython(absorb, migrations.RunPython.noop),
        migrations.DeleteModel(name="Asset"),
        migrations.DeleteModel(name="Liability"),
        migrations.DeleteModel(name="Debt"),
    ]
