"""
Programas de lealtad de tarjetas: puntos, cashback y descuento.

Todo lo de acá (Bank, CategoryType, CardProduct, LoyaltyProgram,
LoyaltyCategoryRate) es catálogo GLOBAL, sin `workspace` -- mismo patrón que
`email_import.BankEmailSchema`: lo lee cualquier usuario autenticado, sólo
staff lo edita (ver `IsAdminOrReadOnly` en api.py). Es lo único que puede ser
así: las categorías reales son por workspace (`transactions.Category`), por
eso las tasas se definen por `CategoryType` (un catálogo estándar de rubros:
"Supermercado", "Gasolina"...) y cada categoría de cada workspace se mapea a
uno de ellos para heredar la tasa que le corresponda (ver
`Category.category_type`).

`LoyaltyEarning` sí es por workspace (lo que efectivamente generó una
transacción real). Puntos y cashback se recalculan solos con la transacción
que los originó (ver `signals.py`); descuento lo registra el propio cliente
al aplicar el descuento sugerido en el formulario (ver
`apps.transactions.api.TransactionSerializer`), porque ahí sí hace falta el
monto de ANTES del descuento -- algo que ya no está en la transacción
guardada (esta queda con el monto neto, a diferencia del cashback que no
toca el monto de la compra).
"""
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.workspaces.models import Workspace


class Bank(BaseModel):
    name = models.CharField("nombre", max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "banco"
        verbose_name_plural = "bancos"

    def __str__(self):
        return self.name


class CategoryType(BaseModel):
    """Rubro estándar (Supermercado, Gasolina, Restaurantes...), independiente
    de las categorías de cada workspace -- ver docstring del módulo. Paso 1
    del flujo: creá los rubros que te interesen ACÁ antes que nada -- son
    los que después vas a poder elegir tanto al definir una tasa especial
    (más abajo, en un Programa de lealtad) como al mapear una categoría real
    de la app (Herramientas → esa categoría → "Rubro para tarjetas con
    recompensas")."""

    slug = models.SlugField("slug", max_length=50, unique=True)
    name = models.CharField("nombre", max_length=100)
    icon = models.CharField("ícono (emoji, opcional)", max_length=50, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "rubro"
        verbose_name_plural = "rubros"

    def __str__(self):
        return self.name


class Merchant(BaseModel):
    """Un comercio conocido (Súper Selectos, McDonald's, Cinemark…): sirve para
    dos cosas que un rubro solo no resuelve.

    1. **Beneficios de un solo comercio** (7 % en Selectos, 2 millas en
       Avianca): una `LoyaltyCategoryRate` puede apuntar a un comercio en vez
       de a un rubro.
    2. **Rubro más fino que la categoría del usuario**: una categoría "Comida"
       mezcla restaurantes con supermercados. Si el comercio se reconoce en la
       descripción de la transacción, su rubro manda sobre el de la categoría.

    El reconocimiento es por `aliases` (ver `services.match_merchant`): no hay
    campo de comercio en la transacción, sólo su descripción libre."""

    name = models.CharField("nombre", max_length=100, unique=True)
    category_type = models.ForeignKey(
        CategoryType, on_delete=models.SET_NULL, null=True, blank=True, related_name="merchants",
        verbose_name="rubro", help_text="Vacío si el comercio no encaja en ningún rubro.",
    )
    aliases = models.TextField(
        "alias", blank=True,
        help_text=(
            "Cómo aparece en la descripción de una transacción, uno por línea "
            "(sin importar mayúsculas ni tildes). Cuenta si aparece como palabra "
            "completa: \"mc\" reconoce \"mc combo\" pero no \"mcdonalds\"."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "comercio"
        verbose_name_plural = "comercios"

    def __str__(self):
        return self.name

    @property
    def alias_list(self) -> list[str]:
        return [a.strip() for a in [self.name, *self.aliases.splitlines()] if a.strip()]


class CardProduct(BaseModel):
    NETWORK_VISA = "visa"
    NETWORK_MASTERCARD = "mastercard"
    NETWORK_AMEX = "amex"
    NETWORK_OTHER = "other"
    NETWORK_CHOICES = [
        (NETWORK_VISA, "Visa"),
        (NETWORK_MASTERCARD, "Mastercard"),
        (NETWORK_AMEX, "American Express"),
        (NETWORK_OTHER, "Otra"),
    ]

    bank = models.ForeignKey(Bank, on_delete=models.CASCADE, related_name="products", verbose_name="banco")
    name = models.CharField("nombre", max_length=100)
    network = models.CharField("red", max_length=12, choices=NETWORK_CHOICES, default=NETWORK_OTHER)

    class Meta:
        ordering = ["bank__name", "name"]
        verbose_name = "producto de tarjeta"
        verbose_name_plural = "productos de tarjeta"
        constraints = [
            models.UniqueConstraint(fields=["bank", "name"], name="unique_product_per_bank"),
        ]

    def __str__(self):
        return f"{self.bank.name} {self.name}"


class LoyaltyProgram(BaseModel):
    """Un mecanismo de recompensa de un producto. Un mismo `CardProduct` puede
    tener varios a la vez (p. ej. uno de puntos + uno de descuento) -- cada
    uno con su propia tasa default y sus overrides por rubro.

    Paso 2 del flujo (después de crear el Banco, el Producto y los Rubros
    que necesites): creá acá el programa con su tasa default, GUARDALO, y
    volvé a entrar a este mismo programa -- ahí abajo vas a ver "Tasas por
    rubro", donde agregás la tasa especial para cada rubro que la tenga
    distinta de la default (p. ej. 1% default, pero 5% en Supermercado)."""

    KIND_POINTS = "points"
    KIND_CASHBACK = "cashback"
    KIND_DISCOUNT = "discount"
    KIND_CHOICES = [
        (KIND_POINTS, "Puntos"),
        (KIND_CASHBACK, "Cashback"),
        (KIND_DISCOUNT, "Descuento"),
    ]

    card_product = models.ForeignKey(
        CardProduct, on_delete=models.CASCADE, related_name="programs", verbose_name="producto de tarjeta"
    )
    kind = models.CharField("tipo", max_length=10, choices=KIND_CHOICES)
    name = models.CharField(
        "nombre (opcional)", max_length=100, blank=True,
        help_text="Para identificarlo si un producto tiene más de un programa del mismo tipo.",
    )
    default_rate = models.DecimalField(
        "tasa default", max_digits=6, decimal_places=4, default=Decimal("0"),
        help_text=(
            "Puntos: puntos ganados por unidad de moneda gastada (p. ej. 2 = "
            "\"2 puntos por dólar\"). Cashback/descuento: fracción del monto "
            "(0.01 = 1%, 0.05 = 5%). Se usa para cualquier rubro que no tenga "
            "una tasa especial en \"Tasas por rubro\" (abajo, guardando primero)."
        ),
    )
    point_value = models.DecimalField(
        "valor de canje por punto (opcional)", max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Sólo puntos: valor estimado por punto (p. ej. 0.01 = 1 punto vale 1 centavo). Sólo referencia, no afecta ningún cálculo real.",
    )
    min_amount = models.DecimalField(
        "compra mínima (opcional)", max_digits=14, decimal_places=2, null=True, blank=True,
        help_text=(
            "Sólo las compras de este monto o más ganan (p. ej. 10 = \"a partir de $10\"). "
            "Vacío = cualquier monto. Aplica a puntos y cashback; el descuento lo aplica "
            "el propio cliente al registrar el gasto."
        ),
    )
    is_active = models.BooleanField("activo", default=True)

    class Meta:
        ordering = ["card_product", "kind"]
        verbose_name = "programa de lealtad"
        verbose_name_plural = "programas de lealtad"

    def __str__(self):
        return self.name or f"{self.card_product} ({self.get_kind_display()})"

    def qualifies(self, amount: Decimal) -> bool:
        """Si una compra de ese monto llega a la compra mínima del programa."""
        return self.min_amount is None or amount >= self.min_amount

    def rate_for(
        self, category_type, on: date | None = None, merchant=None, autopay: bool = False
    ) -> Decimal:
        """Tasa efectiva para una compra: gana la regla más específica.

        1. el comercio, ese día de la semana
        2. el comercio, todos los días
        3. el rubro, ese día de la semana
        4. el rubro, todos los días
        5. la `default_rate` del programa

        Sin `on`, las reglas que dependen del día no se consideran; sin
        `merchant` (o si el programa no tiene reglas para él), se pasa a las del rubro.
        Las reglas `requires_autopay` sólo cuentan si la compra es un cargo
        automático (`autopay`), y en ese caso ganan a las de cualquier cargo."""
        rows = list(self.category_rates.all())
        day = on.weekday() if on is not None else None

        def pick(match):
            candidates = [r for r in rows if match(r)]
            for autopay_rule in (True, False) if autopay else (False,):
                pool = [r for r in candidates if r.requires_autopay == autopay_rule]
                if day is not None:
                    for r in pool:
                        if r.weekday == day:
                            return r.rate
                for r in pool:
                    if r.weekday is None:
                        return r.rate
            return None

        if merchant is not None:
            rate = pick(lambda r: r.merchant_id == merchant.pk)
            if rate is not None:
                return rate
        if category_type is not None:
            rate = pick(lambda r: r.category_type_id == category_type.pk)
            if rate is not None:
                return rate
        return self.default_rate


WEEKDAY_CHOICES = [
    (0, "Lunes"), (1, "Martes"), (2, "Miércoles"), (3, "Jueves"),
    (4, "Viernes"), (5, "Sábado"), (6, "Domingo"),
]  # mismos números que `date.weekday()`


class LoyaltyCategoryRate(BaseModel):
    """Tasa especial de un programa para un rubro **o un comercio** puntual
    (p. ej. 5% de cashback en Supermercado en vez del 1% default del programa,
    o 7% sólo en Súper Selectos) -- ésta es la pieza que resuelve "para tal
    categoría, tal % en tal tarjeta". Exactamente uno de `category_type` y
    `merchant`; opcionalmente sólo un día de la semana."""

    program = models.ForeignKey(
        LoyaltyProgram, on_delete=models.CASCADE, related_name="category_rates", verbose_name="programa"
    )
    category_type = models.ForeignKey(
        CategoryType, on_delete=models.CASCADE, related_name="+", verbose_name="rubro",
        null=True, blank=True,
    )
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name="+", verbose_name="comercio",
        null=True, blank=True,
        help_text="Para un beneficio de un solo comercio. Se llena éste **o** el rubro, no los dos.",
    )
    rate = models.DecimalField(
        "tasa", max_digits=6, decimal_places=4,
        help_text="Mismo formato que la tasa default del programa (puntos por unidad, o fracción para cashback/descuento). Reemplaza la default sólo para este rubro o comercio.",
    )
    weekday = models.PositiveSmallIntegerField(
        "día de la semana (opcional)", null=True, blank=True, choices=WEEKDAY_CHOICES,
        help_text="Si se indica, la tasa sólo aplica las compras hechas ese día (p. ej. 2 puntos los lunes en Supermercado). Vacío = todos los días. Gana a la tasa sin día.",
    )

    requires_autopay = models.BooleanField(
        "sólo cargos automáticos", default=False,
        help_text=(
            "La tasa aplica únicamente si el gasto es un cargo automático de la tarjeta "
            "(Pagos Automáticos de servicios): el gasto lo marca así al registrarlo."
        ),
    )

    class Meta:
        verbose_name = "tasa por rubro o comercio"
        verbose_name_plural = "tasas por rubro o comercio"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(category_type__isnull=False, merchant__isnull=True)
                    | models.Q(category_type__isnull=True, merchant__isnull=False)
                ),
                name="rate_has_category_type_xor_merchant",
            ),
            # Una tasa por (programa, rubro o comercio, día). Cuatro constraints
            # porque en Postgres los NULL no chocan entre sí: hace falta una por
            # cada combinación de "con día / sin día" y "rubro / comercio".
            models.UniqueConstraint(
                fields=["program", "category_type", "weekday", "requires_autopay"],
                condition=models.Q(category_type__isnull=False, weekday__isnull=False),
                name="unique_rate_per_program_category_weekday",
            ),
            models.UniqueConstraint(
                fields=["program", "category_type", "requires_autopay"],
                condition=models.Q(category_type__isnull=False, weekday__isnull=True),
                name="unique_rate_per_program_category",
            ),
            models.UniqueConstraint(
                fields=["program", "merchant", "weekday", "requires_autopay"],
                condition=models.Q(merchant__isnull=False, weekday__isnull=False),
                name="unique_rate_per_program_merchant_weekday",
            ),
            models.UniqueConstraint(
                fields=["program", "merchant", "requires_autopay"],
                condition=models.Q(merchant__isnull=False, weekday__isnull=True),
                name="unique_rate_per_program_merchant",
            ),
        ]

    def __str__(self):
        day = f" ({self.get_weekday_display()})" if self.weekday is not None else ""
        return f"{self.program} · {self.merchant or self.category_type}{day}: {self.rate}"


class LoyaltyEarning(BaseModel):
    """Lo que generó una Transaction real según un programa de lealtad.

    - `points` (kind=points): puntos ganados.
    - `amount` (kind=cashback): monto a acreditar después (la transacción
      sigue con su monto completo, sin tocar).
    - `original_amount` (kind=discount): el monto ANTES del descuento (la
      transacción ya quedó con el monto neto); el ahorro se calcula al vuelo
      como `original_amount - transaction.amount` (ver `discount_saved_amount`)
      en vez de guardarse aparte, para no desincronizarse si se edita el
      monto de la transacción más adelante.
    """

    KIND_POINTS = LoyaltyProgram.KIND_POINTS
    KIND_CASHBACK = LoyaltyProgram.KIND_CASHBACK
    KIND_DISCOUNT = LoyaltyProgram.KIND_DISCOUNT
    KIND_CHOICES = LoyaltyProgram.KIND_CHOICES

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="loyalty_earnings")
    transaction = models.ForeignKey(
        "transactions.Transaction", on_delete=models.CASCADE, related_name="loyalty_earnings"
    )
    program = models.ForeignKey(LoyaltyProgram, on_delete=models.CASCADE, related_name="earnings")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    points = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    original_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ganancia de lealtad"
        verbose_name_plural = "ganancias de lealtad"
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "program"], name="unique_earning_per_transaction_program"
            ),
        ]

    @property
    def discount_saved_amount(self) -> Decimal | None:
        if self.kind != self.KIND_DISCOUNT or self.original_amount is None:
            return None
        return self.original_amount - self.transaction.amount

    def __str__(self):
        return f"{self.transaction_id} · {self.program} ({self.kind})"


class LoyaltyMovement(BaseModel):
    """Lo que se hace con las recompensas ya ganadas: **canjearlas** o **ajustarlas**.

    El disponible de una tarjeta y un programa es `ganado + suma de los movimientos`
    (`services.wallet_balances`). Lo ganado sale de `LoyaltyEarning` y se recalcula solo
    con cada gasto, así que un canje o un ajuste NO puede vivir ahí: se perdería. Por eso
    hay un libro aparte, y "editar el disponible" nunca sobrescribe un saldo: agrega un
    renglón (con motivo) que se puede corregir o deshacer.

    `delta` es el efecto sobre el disponible, en la unidad del programa (puntos, o
    dinero para cashback): negativo al canjear, con signo al ajustar."""

    KIND_REDEEM = "redeem"
    KIND_ADJUST = "adjust"
    KIND_CHOICES = [(KIND_REDEEM, "Canje"), (KIND_ADJUST, "Ajuste")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="loyalty_movements")
    wallet = models.ForeignKey("accounts.Wallet", on_delete=models.CASCADE, related_name="loyalty_movements")
    program = models.ForeignKey(LoyaltyProgram, on_delete=models.CASCADE, related_name="movements")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    delta = models.DecimalField(max_digits=14, decimal_places=2)
    # Sólo canjes: lo que valió en dinero (un punto vale distinto el día del canje).
    cash_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    date = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    # Si el canje se depositó en una cartera, el ingreso que se registró.
    deposit_transaction = models.ForeignKey(
        "transactions.Transaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "movimiento de recompensas"
        verbose_name_plural = "movimientos de recompensas"
        constraints = [
            models.CheckConstraint(condition=~models.Q(delta=0), name="loyalty_movement_delta_not_zero"),
            models.CheckConstraint(
                condition=~models.Q(kind="redeem") | models.Q(delta__lt=0),
                name="loyalty_redeem_is_negative",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.delta} · {self.program}"
