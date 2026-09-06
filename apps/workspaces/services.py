"""Borrado, respaldo y restauración de los datos de un workspace.

`wipe_workspace_data` es compartida por `WorkspaceViewSet.reset` (borra sin
reemplazar nada) y por `import_backup` (borra y enseguida repuebla desde un
respaldo) -- así ambos caminos quedan consistentes con un solo lugar que sabe
el orden correcto de borrado.
"""
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Invitation

BACKUP_FORMAT = "budget-app-backup"
BACKUP_VERSION = 1


class BackupError(Exception):
    """Respaldo con formato inválido o incompleto -- se traduce a 400."""


def get_or_create_invitation(workspace, email, role, invited_by):
    """Invitación pendiente para ``email`` en ``workspace``: reutiliza la
    existente (reinvitar = reenviar el mismo enlace) o crea una nueva."""
    invitation = Invitation.objects.filter(
        workspace=workspace, email__iexact=email, status=Invitation.STATUS_PENDING
    ).first()
    if invitation is not None:
        return invitation
    return Invitation.objects.create(
        workspace=workspace, email=email, role=role, invited_by=invited_by
    )


def send_invitation_email(invitation):
    """Correo de invitación vía el `EMAIL_BACKEND` configurado (Mailgun por
    SMTP en producción; consola en dev sin credenciales)."""
    link = f"{settings.INVITE_ACCEPT_URL_BASE}/{invitation.token}"
    if invitation.invited_by:
        invited_by_name = invitation.invited_by.get_full_name() or invitation.invited_by.username
    else:
        invited_by_name = "Alguien"
    send_mail(
        subject=f'Te invitaron a "{invitation.workspace.name}" en Budget',
        message=(
            f"{invited_by_name} te invitó a compartir el presupuesto "
            f'"{invitation.workspace.name}" en Budget.\n\n'
            f"Abrí este enlace desde tu celular para unirte:\n{link}\n\n"
            f"Si todavía no tenés cuenta, registrate primero con este mismo "
            f"correo ({invitation.email}) y después volvé a abrir el enlace."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invitation.email],
    )


def wipe_workspace_data(workspace, scope="movimientos"):
    """Borra datos del workspace. `scope`:

    - ``"movimientos"`` (default): transacciones, recurrentes, cuotas,
      presupuestos y snapshots; deja carteras/categorías/etiquetas y
      resetea el saldo de cada cartera a su ``opening_balance``.
    - ``"todo"``: además borra carteras, categorías y etiquetas.

    Devuelve un dict con la cantidad borrada de cada tipo. No abre su propia
    transacción atómica -- el caller decide (p. ej. `import_backup` la envuelve
    junto con la repoblación, para que un error a mitad de camino no deje el
    workspace vacío).
    """
    from apps.accounts.models import Wallet
    from apps.accounts.services import recompute_wallet_balance
    from apps.reports.models import MonthlySnapshot
    from apps.transactions.models import (
        Category,
        CategoryBudget,
        InstallmentPurchase,
        RecurringExpense,
        Tag,
        Transaction,
    )

    deleted = {}
    deleted["transactions"] = Transaction.all_objects.filter(
        wallet__workspace=workspace
    ).delete()[0]
    deleted["recurring_expenses"] = RecurringExpense.all_objects.filter(
        workspace=workspace
    ).delete()[0]
    deleted["installment_purchases"] = InstallmentPurchase.all_objects.filter(
        workspace=workspace
    ).delete()[0]
    deleted["category_budgets"] = CategoryBudget.all_objects.filter(
        workspace=workspace
    ).delete()[0]
    deleted["monthly_snapshots"] = MonthlySnapshot.all_objects.filter(
        workspace=workspace
    ).delete()[0]

    if scope == "todo":
        deleted["tags"] = Tag.all_objects.filter(workspace=workspace).delete()[0]

        # Carteras: hijas antes que padres (parent es on_delete=PROTECT).
        wallets = Wallet.all_objects.filter(workspace=workspace)
        count = 0
        while wallets.exists():
            leaves = wallets.filter(children__isnull=True)
            if not leaves.exists():
                leaves = wallets  # por si hay ciclos raros
            count += leaves.delete()[0]
        deleted["wallets"] = count

        cats = Category.all_objects.filter(workspace=workspace)
        count = 0
        while cats.exists():
            leaves = cats.filter(subcategories__isnull=True)
            if not leaves.exists():
                leaves = cats
            count += leaves.delete()[0]
        deleted["categories"] = count
    else:
        for wallet in Wallet.objects.filter(workspace=workspace):
            recompute_wallet_balance(wallet)

    return deleted


def _dec(value):
    return str(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


def export_backup(workspace):
    """Dump completo (menos fotos de recibo) del workspace: carteras,
    categorías, etiquetas, presupuestos, recurrentes, compras a plazo y
    transacciones. Pensado para bajarse como archivo y restaurarse después
    con `import_backup` -- distinto del CSV de "Exportar datos" (que sólo
    trae transacciones, para abrir en una hoja de cálculo).

    Conserva el UUID original de cada fila: así las relaciones (la cartera
    de una transacción, la categoría de un presupuesto...) se pueden
    reconstruir tal cual al restaurar, sin tener que re-mapear IDs.
    """
    from apps.accounts.models import Wallet
    from apps.transactions.models import (
        Category,
        CategoryBudget,
        InstallmentPurchase,
        RecurringExpense,
        Tag,
        Transaction,
    )

    wallets = Wallet.objects.filter(workspace=workspace).order_by("sort_order", "name")
    categories = Category.objects.filter(workspace=workspace).order_by("sort_order", "name")
    tags = Tag.objects.filter(workspace=workspace).order_by("name")
    budgets = CategoryBudget.objects.filter(workspace=workspace)
    recurring = RecurringExpense.objects.filter(workspace=workspace)
    installments = InstallmentPurchase.objects.filter(workspace=workspace)
    transactions = (
        Transaction.objects.filter(wallet__workspace=workspace)
        .select_related("created_by")
        .prefetch_related("tags")
        .order_by("date", "created_at")
    )

    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": timezone.now().isoformat(),
        "workspace_name": workspace.name,
        "base_currency": workspace.base_currency,
        "wallets": [
            {
                "id": str(w.id),
                "name": w.name,
                "purpose": w.purpose,
                "kind": w.kind,
                "currency": w.currency,
                "color": w.color,
                "opening_balance": _dec(w.opening_balance),
                "counts_toward_net_worth": w.counts_toward_net_worth,
                "goal_amount": _dec(w.goal_amount),
                "goal_date": _iso(w.goal_date),
                "monthly_contribution": _dec(w.monthly_contribution),
                "credit_limit": _dec(w.credit_limit),
                "card_last4": w.card_last4,
                "billing_cycle_day": w.billing_cycle_day,
                "payment_due_day": w.payment_due_day,
                "interest_rate": _dec(w.interest_rate),
                "due_date": _iso(w.due_date),
                "counterparty": w.counterparty,
                "visibility": w.visibility,
                "owner_username": w.owner.username if w.owner_id else None,
                "is_active": w.is_active,
                "is_archived": w.is_archived,
                "sort_order": w.sort_order,
                "is_default": w.is_default,
                "parent": str(w.parent_id) if w.parent_id else None,
            }
            for w in wallets
        ],
        "categories": [
            {
                "id": str(c.id),
                "name": c.name,
                "icon": c.icon,
                "color": c.color,
                "type": c.type,
                "sort_order": c.sort_order,
                "parent": str(c.parent_id) if c.parent_id else None,
            }
            for c in categories
        ],
        "tags": [{"id": str(t.id), "name": t.name} for t in tags],
        "category_budgets": [
            {
                "id": str(b.id),
                "category": str(b.category_id),
                "amount": _dec(b.amount),
                "month": b.month,
                "year": b.year,
            }
            for b in budgets
        ],
        "recurring_expenses": [
            {
                "id": str(r.id),
                "category": str(r.category_id),
                "wallet": str(r.wallet_id),
                "amount": _dec(r.amount),
                "frequency": r.frequency,
                "next_due_date": _iso(r.next_due_date),
                "is_active": r.is_active,
            }
            for r in recurring
        ],
        "installment_purchases": [
            {
                "id": str(p.id),
                "wallet": str(p.wallet_id),
                "payment_wallet": str(p.payment_wallet_id) if p.payment_wallet_id else None,
                "category": str(p.category_id),
                "description": p.description,
                "total_amount": _dec(p.total_amount),
                "installment_amount": _dec(p.installment_amount),
                "installments_total": p.installments_total,
                "installments_paid": p.installments_paid,
                "start_date": _iso(p.start_date),
            }
            for p in installments
        ],
        "transactions": [
            {
                "id": str(t.id),
                "type": t.type,
                "wallet": str(t.wallet_id),
                "to_wallet": str(t.to_wallet_id) if t.to_wallet_id else None,
                "category": str(t.category_id) if t.category_id else None,
                "amount": _dec(t.amount),
                "description": t.description,
                "date": _iso(t.date),
                "counts_toward_budget": t.counts_toward_budget,
                "source": t.source,
                "is_recurring": t.is_recurring,
                "split_group": str(t.split_group) if t.split_group else None,
                "created_by_username": t.created_by.username if t.created_by_id else None,
                "tags": [str(tag.id) for tag in t.tags.all()],
            }
            for t in transactions
        ],
    }


def _ids_used_elsewhere(model, ids, workspace, workspace_lookup):
    """IDs de `ids` que ya existen en `model` fuera de `workspace` -- chocarían
    al restaurar (los UUID son globales por tabla, no por workspace)."""
    if not ids:
        return []
    return list(
        model.all_objects.filter(id__in=ids)
        .exclude(**{workspace_lookup: workspace})
        .values_list("id", flat=True)
    )


def _check_no_id_collisions(workspace, data):
    """Un respaldo trae los UUID originales de sus filas para que las
    relaciones se puedan reconstruir tal cual -- pero eso significa que solo
    se puede restaurar donde esos IDs no choquen con otra fila ya existente
    (de este mismo workspace no importa: se borra antes de recrear). El caso
    típico que esto evita: restaurar el mismo respaldo dos veces en
    presupuestos distintos que coexisten."""
    from apps.accounts.models import Wallet
    from apps.transactions.models import (
        Category,
        CategoryBudget,
        InstallmentPurchase,
        RecurringExpense,
        Tag,
        Transaction,
    )

    checks = [
        (Wallet, [r["id"] for r in data.get("wallets", [])], "workspace"),
        (Category, [r["id"] for r in data.get("categories", [])], "workspace"),
        (Tag, [r["id"] for r in data.get("tags", [])], "workspace"),
        (CategoryBudget, [r["id"] for r in data.get("category_budgets", [])], "workspace"),
        (RecurringExpense, [r["id"] for r in data.get("recurring_expenses", [])], "workspace"),
        (
            InstallmentPurchase,
            [r["id"] for r in data.get("installment_purchases", [])],
            "workspace",
        ),
        (Transaction, [r["id"] for r in data.get("transactions", [])], "wallet__workspace"),
    ]
    for model, ids, lookup in checks:
        if _ids_used_elsewhere(model, ids, workspace, lookup):
            raise BackupError(
                "Este respaldo no se puede restaurar acá: parte de sus datos ya "
                "existen en otro presupuesto (por ejemplo, si ya se restauró antes, "
                "o si es el presupuesto del que se exportó y todavía tiene datos)."
            )


def _coalesce(row, key, default):
    """`row.get(key, default)`, pero tratando un `null` explícito en el JSON
    igual que si la clave no viniera. Para campos con default sensato (no
    para relaciones, que si vienen en `null` es porque de verdad no aplican)
    -- un respaldo armado a mano (migrando desde otra app) manda `null`
    donde nuestro propio `export_backup` nunca lo haría, y sin esto eso
    termina en un IntegrityError crudo de la base en vez de un error claro."""
    value = row.get(key, default)
    return default if value is None else value


def _check_required_fields(data):
    """Antes de tocar la base: que ninguna fila le falte (o traiga en
    `null`) un campo sin el cual no tiene sentido -- así un respaldo
    incompleto (típico de uno armado a mano) se rechaza con un mensaje
    claro en vez de reventar a mitad de la restauración con un
    IntegrityError crudo."""
    problems = []

    def require(rows, label, fields):
        for i, row in enumerate(rows):
            missing = [f for f in fields if row.get(f) is None]
            if missing:
                problems.append(f"{label}[{i}] (id={row.get('id')!r}): falta {missing}")

    require(data.get("wallets", []), "wallets", ["id", "name"])
    require(data.get("categories", []), "categories", ["id", "name", "type"])
    require(data.get("tags", []), "tags", ["id", "name"])
    require(
        data.get("category_budgets", []),
        "category_budgets",
        ["id", "category", "amount", "month", "year"],
    )
    require(
        data.get("recurring_expenses", []),
        "recurring_expenses",
        ["id", "category", "wallet", "amount", "frequency", "next_due_date"],
    )
    require(
        data.get("installment_purchases", []),
        "installment_purchases",
        [
            "id", "wallet", "category", "description", "total_amount",
            "installment_amount", "installments_total", "start_date",
        ],
    )
    require(data.get("transactions", []), "transactions", ["id", "type", "wallet", "amount", "date"])

    if problems:
        preview = "; ".join(problems[:10])
        more = f" (y {len(problems) - 10} más)" if len(problems) > 10 else ""
        raise BackupError(f"El respaldo tiene filas incompletas: {preview}{more}")


def import_backup(workspace, data, requesting_user):
    """Reemplaza TODO el contenido de `workspace` por lo que trae `data` (un
    dump de `export_backup`): primero borra lo que había (como
    `wipe_workspace_data(workspace, "todo")`), después recrea cada fila
    preservando su UUID original para que las relaciones queden intactas.

    El saldo de cada cartera NO se copia del backup: se reconstruye solo,
    de una sola pasada por cartera (agregando sus transacciones ya
    restauradas) una vez que todas las filas están cargadas -- así el
    restore de paso corrige cualquier saldo que hubiera quedado
    desincronizado. Todo se inserta con `bulk_create`/`bulk_update` (no fila
    por fila) para que un respaldo grande (miles de transacciones) no tarde
    minutos ni arriesgue el timeout del request.

    Un usuario (`owner_username` / `created_by_username`) que ya no es
    miembro del workspace destino (backup restaurado en otra cuenta, por
    ejemplo) cae a `None` en carteras y a `requesting_user` en transacciones,
    en vez de fallar.
    """
    from apps.accounts.models import Wallet
    from apps.accounts.services import recompute_wallet_balance
    from apps.transactions.models import (
        Category,
        CategoryBudget,
        InstallmentPurchase,
        RecurringExpense,
        Tag,
        Transaction,
    )

    if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
        raise BackupError("El archivo no es un respaldo válido de Budget.")

    _check_required_fields(data)
    _check_no_id_collisions(workspace, data)

    members_by_username = {
        m.user.username: m.user
        for m in workspace.memberships.select_related("user").filter(is_deleted=False)
    }

    def resolve_user(username):
        return members_by_username.get(username)

    wallet_rows = data.get("wallets", [])
    cat_rows = data.get("categories", [])
    tag_rows = data.get("tags", [])
    budget_rows = data.get("category_budgets", [])
    recurring_rows = data.get("recurring_expenses", [])
    installment_rows = data.get("installment_purchases", [])
    txn_rows = data.get("transactions", [])

    with db_transaction.atomic():
        wipe_workspace_data(workspace, scope="todo")

        # --- carteras: primera pasada sin `parent`/`is_default`, para no
        # depender de en qué orden vienen padres e hijos en el backup;
        # `bulk_create` en vez de `.save()` fila por fila -- salta el save()
        # de Wallet (que fija current_balance=opening_balance al crear), así
        # que eso se replica acá a mano. ---
        wallets = [
            Wallet(
                id=row["id"],
                workspace=workspace,
                name=row["name"],
                purpose=_coalesce(row, "purpose", Wallet.PURPOSE_SPENDING),
                kind=_coalesce(row, "kind", Wallet.KIND_BANK),
                currency=_coalesce(row, "currency", "USD"),
                color=_coalesce(row, "color", ""),
                opening_balance=row.get("opening_balance") or "0",
                current_balance=row.get("opening_balance") or "0",
                counts_toward_net_worth=_coalesce(row, "counts_toward_net_worth", True),
                goal_amount=row.get("goal_amount"),
                goal_date=row.get("goal_date"),
                monthly_contribution=row.get("monthly_contribution"),
                credit_limit=row.get("credit_limit"),
                card_last4=row.get("card_last4"),
                billing_cycle_day=row.get("billing_cycle_day"),
                payment_due_day=row.get("payment_due_day"),
                interest_rate=row.get("interest_rate"),
                due_date=row.get("due_date"),
                counterparty=_coalesce(row, "counterparty", ""),
                visibility=_coalesce(row, "visibility", Wallet.VISIBILITY_SHARED),
                owner=resolve_user(row.get("owner_username")),
                is_active=_coalesce(row, "is_active", True),
                is_archived=_coalesce(row, "is_archived", False),
                sort_order=_coalesce(row, "sort_order", 0),
                is_default=False,
            )
            for row in wallet_rows
        ]
        Wallet.objects.bulk_create(wallets)

        # Si el backup viniera corrupto con más de una cartera default (no
        # debería, `export_backup` nunca produce eso), gana la última -- el
        # mismo criterio que el `save()` normal, que iba desmarcando a las
        # anteriores a medida que procesaba filas.
        default_id = next(
            (row["id"] for row in reversed(wallet_rows) if row.get("is_default")), None
        )
        to_update = {}
        for w, row in zip(wallets, wallet_rows):
            if row.get("parent"):
                w.parent_id = row["parent"]
                to_update[w.id] = w
            if w.id == default_id:
                w.is_default = True
                to_update[w.id] = w
        if to_update:
            Wallet.objects.bulk_update(to_update.values(), ["parent", "is_default"])

        # --- categorías: mismo patrón para `parent` ---
        categories = [
            Category(
                id=row["id"],
                workspace=workspace,
                name=row["name"],
                icon=_coalesce(row, "icon", ""),
                color=_coalesce(row, "color", ""),
                type=row["type"],
                sort_order=_coalesce(row, "sort_order", 0),
            )
            for row in cat_rows
        ]
        Category.objects.bulk_create(categories)

        to_update = []
        for c, row in zip(categories, cat_rows):
            if row.get("parent"):
                c.parent_id = row["parent"]
                to_update.append(c)
        if to_update:
            Category.objects.bulk_update(to_update, ["parent"])

        # --- etiquetas ---
        Tag.objects.bulk_create(
            [Tag(id=row["id"], workspace=workspace, name=row["name"]) for row in tag_rows]
        )

        # --- presupuestos por categoría ---
        CategoryBudget.objects.bulk_create(
            [
                CategoryBudget(
                    id=row["id"],
                    workspace=workspace,
                    category_id=row["category"],
                    amount=row["amount"],
                    month=row["month"],
                    year=row["year"],
                )
                for row in budget_rows
            ]
        )

        # --- recurrentes ---
        RecurringExpense.objects.bulk_create(
            [
                RecurringExpense(
                    id=row["id"],
                    workspace=workspace,
                    category_id=row["category"],
                    wallet_id=row["wallet"],
                    amount=row["amount"],
                    frequency=row["frequency"],
                    next_due_date=row["next_due_date"],
                    is_active=_coalesce(row, "is_active", True),
                )
                for row in recurring_rows
            ]
        )

        # --- compras a plazo (fila tal cual, sin re-disparar el cargo
        # inicial: la transacción de esa compra ya viene en `transactions`) ---
        InstallmentPurchase.objects.bulk_create(
            [
                InstallmentPurchase(
                    id=row["id"],
                    workspace=workspace,
                    wallet_id=row["wallet"],
                    payment_wallet_id=row.get("payment_wallet"),
                    category_id=row["category"],
                    description=row["description"],
                    total_amount=row["total_amount"],
                    installment_amount=row["installment_amount"],
                    installments_total=row["installments_total"],
                    installments_paid=_coalesce(row, "installments_paid", 0),
                    start_date=row["start_date"],
                )
                for row in installment_rows
            ]
        )

        # --- transacciones: `bulk_create` en vez de `.save()` fila por fila
        # -- con miles de transacciones, una consulta por fila (más la del
        # signal que ajusta el saldo) es justo lo que hacía que restaurar un
        # respaldo grande tardara tanto que el request podía hacer timeout.
        # `bulk_create` no dispara signals ni el save() de Transaction, así
        # que `currency` (normalmente heredada de la cartera) se fija acá a
        # mano; `type` viene tal cual del backup, ya coherente con su
        # categoría en el momento de exportar. ---
        wallet_currency = {row["id"]: _coalesce(row, "currency", "USD") for row in wallet_rows}
        txns = [
            Transaction(
                id=row["id"],
                type=row["type"],
                wallet_id=row["wallet"],
                to_wallet_id=row.get("to_wallet"),
                category_id=row.get("category"),
                amount=row["amount"],
                currency=wallet_currency.get(row["wallet"], "USD"),
                description=_coalesce(row, "description", ""),
                date=row["date"],
                counts_toward_budget=_coalesce(row, "counts_toward_budget", True),
                source=_coalesce(row, "source", Transaction.SOURCE_MANUAL),
                is_recurring=_coalesce(row, "is_recurring", False),
                split_group=row.get("split_group"),
                created_by=resolve_user(row.get("created_by_username")) or requesting_user,
            )
            for row in txn_rows
        ]
        Transaction.objects.bulk_create(txns)

        # Etiquetas de transacción: se inserta la tabla intermedia del m2m
        # directo (`.set()` por transacción sería otra consulta por fila).
        through = Transaction.tags.through
        tag_links = [
            through(transaction_id=row["id"], tag_id=tag_id)
            for row in txn_rows
            for tag_id in (row.get("tags") or [])
        ]
        if tag_links:
            through.objects.bulk_create(tag_links)

        # El saldo no se copia del backup: se recalcula una vez por cartera
        # (agrega sus transacciones ya restauradas), en vez de ir sumando
        # delta a delta transacción por transacción -- mismo resultado, y no
        # escala con la cantidad de movimientos sino con la de carteras.
        for wallet in Wallet.objects.filter(workspace=workspace):
            recompute_wallet_balance(wallet)

    return {
        "wallets": len(wallet_rows),
        "categories": len(cat_rows),
        "tags": len(tag_rows),
        "category_budgets": len(budget_rows),
        "recurring_expenses": len(recurring_rows),
        "installment_purchases": len(installment_rows),
        "transactions": len(txn_rows),
    }
