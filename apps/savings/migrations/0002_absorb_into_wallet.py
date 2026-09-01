"""Fusiona SavingsGoal / ReserveFund en Wallet (purpose=savings) y los borra."""
from django.db import migrations


def absorb(apps, schema_editor):
    Wallet = apps.get_model("accounts", "Wallet")
    SavingsGoal = apps.get_model("savings", "SavingsGoal")
    ReserveFund = apps.get_model("savings", "ReserveFund")

    for g in SavingsGoal.objects.all():
        Wallet.objects.create(
            workspace_id=g.workspace_id,
            name=g.name,
            purpose="savings",
            opening_balance=g.current_amount,
            current_balance=g.current_amount,
            goal_amount=g.target_amount,
            goal_date=g.target_date,
            monthly_contribution=g.monthly_contribution_suggested,
            counts_toward_net_worth=True,
            is_deleted=g.is_deleted,
        )

    for f in ReserveFund.objects.all():
        Wallet.objects.create(
            workspace_id=f.workspace_id,
            name=f.name,
            purpose="savings",
            opening_balance=f.current_amount,
            current_balance=f.current_amount,
            monthly_contribution=f.monthly_contribution,
            counts_toward_net_worth=True,
            is_deleted=f.is_deleted,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("savings", "0001_initial"),
        ("accounts", "0005_absorb_asset_liability_debt"),
    ]

    operations = [
        migrations.RunPython(absorb, migrations.RunPython.noop),
        migrations.DeleteModel(name="SavingsGoal"),
        migrations.DeleteModel(name="ReserveFund"),
    ]
