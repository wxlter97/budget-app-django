import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

PURPOSE_CHOICES = [
    ("spending", "Gasto"),
    ("savings", "Ahorro"),
    ("debt", "Deuda"),
    ("asset", "Activo"),
]

_TYPE_TO_PURPOSE = {
    "checking": "spending",
    "cash": "spending",
    "savings": "savings",
    "credit": "debt",
}


def set_purpose_from_type(apps, schema_editor):
    Wallet = apps.get_model("accounts", "Wallet")
    for w in Wallet.objects.all():
        purpose = _TYPE_TO_PURPOSE.get(w.type, "spending")
        if w.purpose != purpose:
            w.purpose = purpose
            w.save(update_fields=["purpose"])


class Migration(migrations.Migration):

    # El RenameModel debe correr DESPUÉS de que transactions / email_import hayan
    # creado sus FKs a `accounts.account` (si no, el executor puede renombrar el
    # modelo antes y esas migraciones fallan al resolver `accounts.account`).
    dependencies = [
        ("accounts", "0003_account_is_default"),
        ("transactions", "0003_transaction_type_transfer_budget_flag"),
        ("email_import", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel("Account", "Wallet"),
        migrations.RemoveConstraint(
            model_name="wallet", name="one_default_account_per_workspace"
        ),
        migrations.AlterField(
            model_name="wallet",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="wallets",
                to="workspaces.workspace",
            ),
        ),
        migrations.AlterField(
            model_name="wallet",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text="Solo aplica si visibility=private",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="owned_wallets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="children",
                to="accounts.wallet",
                help_text="Cartera padre; el saldo mostrado del padre incluye el de sus hijos.",
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="purpose",
            field=models.CharField(
                choices=PURPOSE_CHOICES, default="spending", max_length=10
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="counts_toward_net_worth",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="wallet",
            name="goal_amount",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=14, null=True
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="goal_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wallet",
            name="monthly_contribution",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=14, null=True
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="interest_rate",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="due_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wallet",
            name="counterparty",
            field=models.CharField(
                blank=True, default="", help_text="Persona/entidad de la deuda", max_length=100
            ),
            preserve_default=False,
        ),
        migrations.RunPython(set_purpose_from_type, migrations.RunPython.noop),
        migrations.RemoveField(model_name="wallet", name="type"),
        migrations.AddConstraint(
            model_name="wallet",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True), ("is_deleted", False)),
                fields=("workspace",),
                name="one_default_wallet_per_workspace",
            ),
        ),
    ]
