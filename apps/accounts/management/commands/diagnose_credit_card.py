"""Reconciliación de una tarjeta de crédito: por qué `total_due` del estado
de cuenta no cuadra con la realidad (o con `current_balance`).

    python manage.py diagnose_credit_card --wallet <uuid> [--as-of YYYY-MM-DD]
    python manage.py diagnose_credit_card --workspace <uuid>   # todas las tarjetas

Imprime, para cada tarjeta:
- opening_balance / current_balance (cacheado) vs. recalculado desde movimientos
- cada Transacción viva con su efecto (con signo) sobre el saldo
- el desglose del estado de cuenta a la fecha (`credit_card_statement`)
- la reconciliación: de dónde sale `total_due` y si la historia cargada
  parece incompleta (p. ej. abonos >> cargos con opening_balance 0).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.accounts.services import (
    _cutoff_on_or_before,
    balance_deltas,
    credit_card_statement,
    recompute_wallet_balance,
)


def _d(x) -> Decimal:
    return Decimal(x or 0).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Reconcilia el estado de cuenta de una tarjeta con sus movimientos."

    def add_arguments(self, parser):
        parser.add_argument("--wallet", help="UUID de la tarjeta a diagnosticar.")
        parser.add_argument("--workspace", help="UUID: diagnostica todas sus tarjetas de crédito.")
        parser.add_argument("--as-of", help="Fecha de consulta (YYYY-MM-DD). Hoy por defecto.")

    def handle(self, *args, **opts):
        from apps.transactions.models import InstallmentPurchase, Transaction

        as_of = None
        if opts.get("as_of"):
            as_of = timezone.datetime.strptime(opts["as_of"], "%Y-%m-%d").date()

        qs = Wallet.all_objects.filter(kind=Wallet.KIND_CREDIT)
        if opts.get("wallet"):
            qs = qs.filter(id=opts["wallet"])
        elif opts.get("workspace"):
            qs = qs.filter(workspace_id=opts["workspace"])
        else:
            raise CommandError("Indicá --wallet o --workspace.")

        wallets = list(qs)
        if not wallets:
            raise CommandError("No se encontró ninguna tarjeta de crédito con ese criterio.")

        for w in wallets:
            self._diagnose(w, as_of, Transaction, InstallmentPurchase)

    def _diagnose(self, w, as_of, Transaction, InstallmentPurchase):
        line = "=" * 72
        self.stdout.write(f"\n{line}\n{w.name}  ({w.currency})  id={w.id}\n{line}")
        self.stdout.write(
            f"opening_balance     : {_d(w.opening_balance):>14}\n"
            f"current_balance (BD): {_d(w.current_balance):>14}   <- valor cacheado que ves en la app\n"
            f"billing_cycle_day   : {w.billing_cycle_day}\n"
            f"payment_due_day     : {w.payment_due_day}"
        )

        txns = list(
            Transaction.objects.filter(wallet=w).order_by("date", "created_at")
        )
        incoming = list(
            Transaction.objects.filter(to_wallet=w, type=Transaction.TYPE_TRANSFER).order_by(
                "date", "created_at"
            )
        )

        # --- recálculo desde cero (igual que recompute_wallet_balance) ---
        recomputed = w.opening_balance
        for t in txns:
            recomputed += balance_deltas(t).get(w.id, Decimal("0"))
        for t in incoming:
            recomputed += balance_deltas(t).get(w.id, Decimal("0"))
        flag = "" if _d(recomputed) == _d(w.current_balance) else "   <<< NO COINCIDE con current_balance"
        self.stdout.write(f"current_balance recalculado: {_d(recomputed):>14}{flag}")
        if flag:
            self.stdout.write(
                self.style.WARNING(
                    "  -> El saldo cacheado está desincronizado. Corré: "
                    "python manage.py recompute_balances"
                )
            )

        # --- movimientos ---
        self.stdout.write(f"\nMOVIMIENTOS ({len(txns) + len(incoming)} vivos)")
        self.stdout.write(f"  {'fecha':<11} {'tipo':<9} {'origen':<11} {'efecto':>12}  detalle")
        rows = []
        for t in txns:
            rows.append((t.date, t.type, t.source, balance_deltas(t).get(w.id, Decimal("0")), t.description or ""))
        for t in incoming:
            rows.append(
                (t.date, "transfer→", t.source, balance_deltas(t).get(w.id, Decimal("0")),
                 f"(entra de otra cartera) {t.description or ''}")
            )
        rows.sort(key=lambda r: (r[0], r[1]))
        pos = neg = Decimal("0")
        for d, typ, src, eff, desc in rows:
            if eff >= 0:
                pos += eff
            else:
                neg += eff
            self.stdout.write(f"  {d!s:<11} {typ:<9} {src:<11} {_d(eff):>12}  {desc[:40]}")
        self.stdout.write(
            f"\n  suma de cargos (efecto negativo) : {_d(neg):>14}\n"
            f"  suma de abonos (efecto positivo) : {_d(pos):>14}\n"
            f"  neto movimientos                 : {_d(pos + neg):>14}"
        )

        # --- compras a plazo ---
        purchases = list(InstallmentPurchase.objects.filter(wallet=w))
        purchases += list(InstallmentPurchase.objects.filter(payment_wallet=w))
        if purchases:
            self.stdout.write("\nCOMPRAS A PLAZO")
            for p in purchases:
                mode = "tarjeta (cargo total al inicio)" if p.is_credit_card else "cuota mensual = gasto"
                self.stdout.write(
                    f"  {p.description}: {p.installments_paid}/{p.installments_total} pagadas, "
                    f"cuota {_d(p.installment_amount)}, total {_d(p.total_amount)}, "
                    f"inicio {p.start_date}  [{mode}]"
                )

        # --- estado de cuenta ---
        data = credit_card_statement(w, as_of=as_of)
        if data is None:
            self.stdout.write(
                self.style.WARNING("\nSin billing_cycle_day: la app no genera estado de cuenta para esta tarjeta.")
            )
            return

        eff_as_of = as_of or timezone.localdate()
        cutoff = _cutoff_on_or_before(w.billing_cycle_day, eff_as_of)
        self.stdout.write(f"\nESTADO DE CUENTA  (consulta al {eff_as_of}, corte {cutoff})")
        for k in (
            "spent", "paid", "installments_due", "total_due",
            "current_period_spent", "current_period_paid",
        ):
            self.stdout.write(f"  {k:<22}: {_d(data[k]):>14}")

        due = _d(data["total_due"])
        self.stdout.write(
            "\n  total_due > 0  = tenés que pagar esa cantidad\n"
            "  total_due < 0  = la app cree que pagaste de más (saldo a favor)"
        )
        if due < 0:
            self.stdout.write(
                self.style.WARNING(
                    "\n  DIAGNÓSTICO: total_due es negativo. Casi siempre significa que la\n"
                    "  historia cargada tiene más abonos que cargos porque la tarjeta ya\n"
                    "  tenía deuda cuando empezaste a registrarla y opening_balance quedó\n"
                    f"  en {_d(w.opening_balance)}. Poné opening_balance = -(deuda real el día del primer\n"
                    "  movimiento que cargaste) y volvé a consultar."
                )
            )
