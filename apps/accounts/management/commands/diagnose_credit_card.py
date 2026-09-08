"""Reconciliación de una tarjeta de crédito: por qué `total_due` del estado
de cuenta no cuadra con lo esperado.

    python manage.py diagnose_credit_card --wallet <uuid> [--as-of YYYY-MM-DD]
    python manage.py diagnose_credit_card --workspace <uuid>   # todas las tarjetas

Imprime, para cada tarjeta:
- opening_balance / current_balance (cacheado) vs. recalculado desde movimientos
- cada Transacción viva con su efecto (con signo) sobre el saldo
- el desglose del estado de cuenta a la fecha, con la reconciliación
  `total_due = gastos - abonos + cuotas_vencidas - opening_balance`
- los movimientos del período abierto (después del corte), que alimentan
  `current_period_spent` / `current_period_paid`
- avisos: `source=installment` sueltos, transferencias "ajuste", etc.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Wallet
from apps.accounts.services import (
    _cutoff_on_or_before,
    balance_deltas,
    credit_card_statement,
)


def _d(x) -> Decimal:
    return Decimal(x or 0).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Reconcilia el estado de cuenta de una tarjeta con sus movimientos."

    def add_arguments(self, parser):
        parser.add_argument("--wallet", help="UUID de la tarjeta a diagnosticar.")
        parser.add_argument("--workspace", help="UUID: todas sus tarjetas de crédito.")
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
            raise CommandError("No se encontró ninguna tarjeta con ese criterio.")

        for w in wallets:
            self._diagnose(w, as_of, Transaction, InstallmentPurchase)

    def _diagnose(self, w, as_of, Transaction, InstallmentPurchase):
        line = "=" * 72
        self.stdout.write(f"\n{line}\n{w.name}  ({w.currency})  id={w.id}\n{line}")

        eff_as_of = as_of or timezone.localdate()
        cutoff = (
            _cutoff_on_or_before(w.billing_cycle_day, eff_as_of)
            if w.billing_cycle_day
            else None
        )

        self.stdout.write(
            f"opening_balance     : {_d(w.opening_balance):>14}\n"
            f"current_balance (BD): {_d(w.current_balance):>14}   <- lo que ves como saldo en la app\n"
            f"billing_cycle_day   : {w.billing_cycle_day}     corte usado: {cutoff}\n"
            f"payment_due_day     : {w.payment_due_day}"
        )

        txns = list(Transaction.objects.filter(wallet=w).order_by("date", "created_at"))
        incoming = list(
            Transaction.objects.filter(
                to_wallet=w, type=Transaction.TYPE_TRANSFER
            ).order_by("date", "created_at")
        )
        rows = []
        for t in txns:
            rows.append([t.date, t.type, t.source, balance_deltas(t).get(w.id, Decimal("0")),
                         t.description or "", t])
        for t in incoming:
            rows.append([t.date, "transfer<-", t.source, balance_deltas(t).get(w.id, Decimal("0")),
                         f"(entra de otra cartera) {t.description or ''}", t])
        rows.sort(key=lambda r: (r[0], str(r[1])))

        recomputed = w.opening_balance + sum((r[3] for r in rows), Decimal("0"))
        flag = "" if _d(recomputed) == _d(w.current_balance) else "   <<< NO COINCIDE"
        self.stdout.write(f"current_balance recalculado: {_d(recomputed):>14}{flag}")
        if flag:
            self.stdout.write(self.style.WARNING("  -> corré: python manage.py recompute_balances"))

        self.stdout.write(f"\nMOVIMIENTOS ({len(rows)} vivos)")
        self.stdout.write(f"  {'fecha':<11} {'tipo':<11} {'origen':<11} {'efecto':>12}  detalle")
        for d, typ, src, eff, desc, _t in rows:
            mark = ""
            if src == Transaction.SOURCE_INSTALLMENT:
                mark = "  [cuota/plazo: NO entra en gastos/abonos]"
            elif "ajuste" in desc.lower():
                mark = "  [¿ajuste manual?]"
            self.stdout.write(f"  {d!s:<11} {typ:<11} {src:<11} {_d(eff):>12}  {desc[:38]}{mark}")

        purchases = list(InstallmentPurchase.objects.filter(wallet=w)) + list(
            InstallmentPurchase.objects.filter(payment_wallet=w)
        )
        if purchases:
            self.stdout.write("\nCOMPRAS A PLAZO")
            for p in purchases:
                mode = "financiada con la tarjeta" if p.is_credit_card else "cuota = gasto en la tarjeta"
                n_cal = (
                    sum(1 for n in range(1, p.installments_total + 1) if _months_ok(p, n, cutoff))
                    if cutoff
                    else "?"
                )
                self.stdout.write(
                    f"  {p.description}: installments_paid={p.installments_paid}/{p.installments_total}, "
                    f"cuota {_d(p.installment_amount)}, total {_d(p.total_amount)}, inicio {p.start_date}  [{mode}]\n"
                    f"     cuotas vencidas al corte según el calendario (start_date + N meses): {n_cal}"
                )

        data = credit_card_statement(w, as_of=as_of)
        if data is None:
            self.stdout.write(self.style.WARNING("\nSin billing_cycle_day: no hay estado de cuenta."))
            return

        self.stdout.write(f"\nESTADO DE CUENTA  (consulta al {eff_as_of}, corte {data['cutoff_date']})")
        self.stdout.write(
            f"  gastos normales al corte (spent)      : {_d(data['spent']):>12}\n"
            f"  abonos normales al corte (paid)       : {_d(data['paid']):>12}\n"
            f"  cuotas a plazo ya vencidas            : {_d(data['installments_due']):>12}\n"
            f"  opening_balance                       : {_d(data['opening_balance']):>12}\n"
            f"  --------------------------------------------------------\n"
            f"  total_due = {_d(data['spent'])} - {_d(data['paid'])} + "
            f"{_d(data['installments_due'])} - ({_d(data['opening_balance'])})\n"
            f"  TOTAL A PAGAR (total_due)             : {_d(data['total_due']):>12}\n\n"
            f"  período abierto: gastado {_d(data['current_period_spent'])}, "
            f"abonado {_d(data['current_period_paid'])}"
        )

        after = [r for r in rows if r[0] > data["cutoff_date"]]
        if after:
            self.stdout.write("\n  MOVIMIENTOS DESPUÉS DEL CORTE (arman el 'período abierto'):")
            for d, typ, src, eff, desc, _t in after:
                self.stdout.write(f"    {d!s:<11} {typ:<11} {_d(eff):>12}  {desc[:44]}")

        if _d(w.opening_balance) == 0 and _d(recomputed) > 0:
            self.stdout.write(
                self.style.WARNING(
                    "\n  AVISO: opening_balance=0 y el saldo recalculado es POSITIVO "
                    "(saldo a favor).\n  Si la tarjeta ya tenía deuda cuando empezaste a "
                    "registrarla, poné opening_balance = -(esa deuda)."
                )
            )


def _months_ok(purchase, n, cutoff):
    from dateutil.relativedelta import relativedelta

    return purchase.start_date + relativedelta(months=n - 1) <= cutoff
