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
    installment_status,
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

        purchases = list(InstallmentPurchase.objects.filter(wallet=w))
        if purchases:
            self.stdout.write("\nCOMPRAS A PLAZO")
            for p in purchases:
                status = installment_status(p, as_of=cutoff or eff_as_of)
                next_due = status["next_due_date"]
                next_line = (
                    f"próxima cuota: {_d(status['current_installment_amount'])} vence {next_due}"
                    if next_due
                    else "completa"
                )
                self.stdout.write(
                    f"  {p.description}: {status['installments_paid']}/{p.installments_total} cuotas "
                    f"vencidas al corte, total {_d(p.total_amount)}, inicio {p.start_date}\n"
                    f"     {next_line}"
                )

        data = credit_card_statement(w, as_of=as_of)
        if data is None:
            self.stdout.write(self.style.WARNING("\nSin billing_cycle_day: no hay estado de cuenta."))
            return

        self.stdout.write(f"\nPAGO DE CONTADO  (consulta al {eff_as_of}, corte {data['cutoff_date']})")
        lim = data["credit_limit"]
        avail = data["available"]
        self.stdout.write(
            f"  límite de la tarjeta                  : {_d(lim) if lim is not None else '(sin configurar)':>12}\n"
            f"  disponible (límite + saldo)           : {_d(avail) if avail is not None else '(n/a)':>12}\n"
            f"  saldo usado (límite - disponible)     : {_d(data['used']):>12}\n"
            f"  - capital a plazo aún no vencido      : {_d(data['installments_not_due']):>12}\n"
            f"  --------------------------------------------------------\n"
            f"  PAGO DE CONTADO (total_due)           : {_d(data['total_due']):>12}"
        )

        if data["installment_lines"]:
            self.stdout.write("\n  Cuotas a plazo aún no vencidas:")
            for ln in data["installment_lines"]:
                self.stdout.write(
                    f"    {ln['description'][:34]:<34} "
                    f"{ln['installments_pending']} cuota(s) = {_d(ln['amount_pending'])}"
                )

        after = [r for r in rows if cutoff and r[0] > cutoff]
        if after:
            self.stdout.write("\n  MOVIMIENTOS DESPUÉS DEL CORTE (bajan/suben el saldo usado de hoy):")
            for d, typ, src, eff, desc, _t in after:
                self.stdout.write(f"    {d!s:<11} {typ:<11} {_d(eff):>12}  {desc[:44]}")

        # Chequeo: el saldo usado debería cuadrar con (límite - disponible real del banco).
        self.stdout.write(
            "\n  Comprobá contra tu banca en línea: 'saldo usado' de arriba debe ser "
            "(límite - disponible real).\n  Si no cuadra, faltan/sobran movimientos en la tarjeta "
            "(revisá la lista de arriba)."
        )
