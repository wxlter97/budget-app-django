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
from decimal import Decimal

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
    is_active = models.BooleanField("activo", default=True)

    class Meta:
        ordering = ["card_product", "kind"]
        verbose_name = "programa de lealtad"
        verbose_name_plural = "programas de lealtad"

    def __str__(self):
        return self.name or f"{self.card_product} ({self.get_kind_display()})"

    def rate_for(self, category_type) -> Decimal:
        """Tasa efectiva para un rubro: el override de `LoyaltyCategoryRate`
        si existe, si no la `default_rate` del programa."""
        if category_type is not None:
            override = self.category_rates.filter(category_type=category_type).first()
            if override is not None:
                return override.rate
        return self.default_rate


class LoyaltyCategoryRate(BaseModel):
    """Tasa especial de un programa para un rubro puntual (p. ej. 5% de
    cashback en Supermercado en vez del 1% default del programa) -- ésta es
    la pieza que resuelve "para tal categoría, tal % en tal tarjeta"."""

    program = models.ForeignKey(
        LoyaltyProgram, on_delete=models.CASCADE, related_name="category_rates", verbose_name="programa"
    )
    category_type = models.ForeignKey(
        CategoryType, on_delete=models.CASCADE, related_name="+", verbose_name="rubro"
    )
    rate = models.DecimalField(
        "tasa", max_digits=6, decimal_places=4,
        help_text="Mismo formato que la tasa default del programa (puntos por unidad, o fracción para cashback/descuento). Reemplaza la default sólo para este rubro.",
    )

    class Meta:
        verbose_name = "tasa por rubro"
        verbose_name_plural = "tasas por rubro"
        constraints = [
            models.UniqueConstraint(
                fields=["program", "category_type"], name="unique_rate_per_program_category"
            ),
        ]

    def __str__(self):
        return f"{self.program} · {self.category_type}: {self.rate}"


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
