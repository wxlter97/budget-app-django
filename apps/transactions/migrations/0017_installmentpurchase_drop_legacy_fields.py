# Separada de 0016 a propósito -- ver el docstring de esa migración
# (Postgres no permite ALTER TABLE sobre una tabla en la misma transacción
# en la que se le acaba de hacer un DELETE con FKs referenciándola).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0016_installments_statement_only"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="installmentpurchase",
            name="payment_wallet",
        ),
        migrations.RemoveField(
            model_name="installmentpurchase",
            name="installment_amount",
        ),
        migrations.RemoveField(
            model_name="installmentpurchase",
            name="installments_paid",
        ),
    ]
