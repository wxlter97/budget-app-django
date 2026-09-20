"""
Catálogo de lealtad de los bancos de El Salvador, como datos.

Lo carga `manage.py seed_loyalty_catalog` (repetible: actualiza lo que ya está y
crea lo que falta, no duplica). Fuentes: las páginas públicas de cada banco,
leídas en septiembre de 2026; las tasas cambian, así que si un banco las cambia
se edita ACÁ y se vuelve a correr el comando (o desde el admin, que gana hasta la
próxima corrida).

Convenciones (las mismas de `LoyaltyProgram`):
- puntos y millas: unidades por dólar (`2` = "2 puntos por $1"; `0.5` = "1 por $2").
- cashback y descuento: fracción del monto (`0.05` = 5 %).
- una tasa de rubro es `(slug del rubro, tasa)` o `(slug, tasa, día)` con el día
  como en `date.weekday()`: 0 = lunes … 6 = domingo.

Lo que a propósito NO está (no cabe en el modelo o no se pudo verificar):
- beneficios de un solo comercio de los que no está claro el alcance (Walmart
  "7 % de ahorro", Tigo 20 %): los que sí están claros (Selectos, PriceSmart, San
  Nicolás, Avianca) se cargan como tasa de comercio (`merchants=` en cada programa);
- tarjetas cuyo sitio sólo dice "hasta N" o no publica la tasa;
- Davivienda por producto, Industrial (salvo Premium), ABANK y Apoyo Integral:
  sin datos verificables en su sitio. De Cuscatlán faltan las tarjetas cuya página
  no se pudo abrir (Cash Back Mastercard, Black, MultiPuntos+ Clásica/Platinum,
  lifemiles Real/Oro, Selectos Oro).
"""

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ANY_DAY = None  # el día de una tasa que vale todos los días


# (slug, nombre). Los cinco primeros ya existían.
CATEGORY_TYPES = [
    ("agua", "Agua"),
    ("electricidad", "Electricidad"),
    ("gasolina", "Gasolina"),
    ("supermercado", "Supermercado"),
    ("suscripciones", "Suscripciones"),  # incluye streaming de video y audio
    ("restaurantes", "Restaurantes"),
    ("comida-rapida", "Comida rápida"),
    ("farmacia", "Farmacias"),
    ("viajes", "Viajes (aerolíneas, hoteles, renta de autos, agencias)"),
    ("ferreteria", "Ferreterías"),
    ("almacenes", "Almacenes y centros comerciales"),
    ("calzado", "Zapaterías"),
    ("transporte", "Transporte"),
    ("gaming", "Videojuegos"),
    ("telefonia-internet", "Telefonía e internet"),
    ("compras-en-linea", "Compras en línea (marketplaces)"),
    ("delivery", "Apps de domicilio"),
    ("conciertos-eventos", "Conciertos, teatro y eventos deportivos"),
    ("cines", "Cines"),
    ("gimnasio-deporte", "Gimnasios y deporte"),
    ("tecnologia", "Computación y electrónica"),
    ("educacion", "Educación"),
]

# (nombre, rubro, alias). Comercios que se reconocen en la descripción de una
# transacción (el nombre también cuenta como alias). Sirven para dos cosas:
# beneficios de un solo comercio y afinar el rubro de una categoría amplia
# ("Comida" mezcla restaurantes con supermercados). Los alias se sacaron de lo que
# de verdad se escribe en las transacciones; se agregan más desde el admin.
MERCHANTS = [
    ("Súper Selectos", "supermercado", ["selectos", "super selectos"]),
    ("Walmart", "supermercado", ["wal mart", "walmart"]),
    ("La Despensa de Don Juan", "supermercado", ["despensa", "despensa de don juan", "maxi despensa"]),
    ("PriceSmart", "supermercado", ["price smart", "pricesmart"]),
    ("Farmacias San Nicolás", "farmacia", ["san nicolas", "farmacia san nicolas"]),
    ("Farmacias Económicas", "farmacia", ["farmacia economica", "farmacias economicas"]),
    ("McDonald's", "comida-rapida", ["mc", "mcd", "mcdonalds", "mcdonald s"]),
    ("Wendy's", "comida-rapida", ["wendys", "wendy s"]),
    ("Pizza Hut", "comida-rapida", ["hut"]),
    ("Pollo Campero", "comida-rapida", ["campero"]),
    ("Panda Express", "comida-rapida", []),
    ("Chipotle", "comida-rapida", []),
    ("Shake Shack", "comida-rapida", []),
    ("Raising Cane's", "comida-rapida", ["raising canes"]),
    ("Denny's", "restaurantes", ["dennys", "denny s"]),
    ("Starbucks", "restaurantes", []),
    ("Cinemark", "cines", []),
    ("Cinépolis", "cines", ["cinepolis"]),
    ("Uber", "transporte", []),
    ("Uber Eats", "delivery", []),
    ("PedidosYa", "delivery", ["pedidos ya"]),
    ("Netflix", "suscripciones", []),
    ("Spotify", "suscripciones", []),
    ("Disney+", "suscripciones", ["disney plus", "disney"]),
    ("Avianca", "viajes", []),
    ("United Airlines", "viajes", ["united"]),
    ("Tigo", "telefonia-internet", []),
    # Sin rubro propio: un café en Pronto sigue siendo "Comida"; el comercio sólo
    # sirve para el descuento de UNO.
    ("Tiendas Pronto", None, ["pronto"]),
    # Las gasolineras UNO (descuento de la tarjeta UNO de Cuscatlán). Un alias tan corto
    # sólo cuenta como palabra entera, y quien no lo escriba no recibe el descuento.
    ("Gasolineras UNO", "gasolina", ["uno", "gasolinera uno", "gasolineras uno"]),
    # Sin gasolineras a propósito: un café en Puma o Shell se escribe con el nombre de
    # la gasolinera y caería en el rubro gasolina. Para eso ya está la categoría Gasolina.
    ("Amazon Prime", "suscripciones", ["prime video"]),
    ("Amazon", "compras-en-linea", []),
    ("Temu", "compras-en-linea", []),
    ("Shein", "compras-en-linea", []),
    ("AliExpress", "compras-en-linea", ["ali express"]),
]

# Categorías de un workspace (por nombre, sin importar mayúsculas ni tildes) ->
# rubro. Sólo llena las que no tienen rubro todavía (`map_categories_to_rubros`).
# Las que se dejaron fuera a propósito no tienen un rubro que les sirva:
# Ahorro, Vivienda, Regalos, Ropa, Salud, Parking, Impuestos, Miscelánea…
CATEGORY_TO_RUBRO = {
    "agua": "agua",
    "electricidad": "electricidad",
    "gasolina": "gasolina",
    "supermercado": "supermercado",
    "restaurantes": "restaurantes",
    # "Comida" mezcla restaurantes, comida rápida y súper: el rubro por defecto es
    # restaurantes y el comercio reconocido en la descripción lo afina.
    "comida": "restaurantes",
    "suscripciones": "suscripciones",
    "farmacia": "farmacia",
    "educacion": "educacion",
    "gimnasio": "gimnasio-deporte",
    "deporte": "gimnasio-deporte",
    "telefono": "telefonia-internet",
    "internet": "telefonia-internet",
    "transporte": "transporte",
    "transporte publico": "transporte",
}

# (banco, nombre anterior, nombre nuevo): productos que ya estaban cargados a mano
# con otro nombre. Se renombran (no se crean de nuevo) para no perder las
# tarjetas de los usuarios que ya los tienen asignados.
RENAMES = [
    ("Banco Agrícola", "Tarjeta Oro", "Tarjeta Dorada Visa"),
    ("Banco de América Central", "CASHBACK BLUE", "American Express Blue"),
    ("Banco de América Central", "Economía", "EconoMía Clásica"),
]


def P(kind, default, name="", rates=(), point_value=None, active=True, merchants=(), min_amount=None):
    """`rates`: tasas por rubro, `(slug, tasa)`, `(slug, tasa, día)` o
    `(slug, tasa, día, True)` si sólo vale para cargos automáticos (`AUTO`);
    `merchants`: tasas de un solo comercio, `(nombre del comercio, tasa)` o
    `(nombre, tasa, día)`. `min_amount`: compra mínima para ganar."""
    return {
        "kind": kind, "name": name, "default": default, "rates": list(rates),
        "point_value": point_value, "active": active, "merchants": list(merchants),
        "min_amount": min_amount,
    }


def prod(name, network, *programs):
    return {"name": name, "network": network, "programs": list(programs)}


PB = "Puntos Bancoagrícola"
PB_VALUE = 0.005  # sin valor oficial (el banco lo calcula el día del canje); estimación del dueño

# Bonos de los lunes / miércoles / viernes de las Dorada y Platinum de Agrícola.
_AGRICOLA_DIAS = [
    ("supermercado", 2, MON), ("gasolina", 2, WED),
    ("ferreteria", 2, FRI), ("almacenes", 2, FRI), ("calzado", 2, FRI),
]
_JOVEN_3 = ["suscripciones", "transporte", "gaming", "telefonia-internet",
            "compras-en-linea", "delivery", "conciertos-eventos"]
_JOVEN_2 = ["gimnasio-deporte", "comida-rapida", "tecnologia", "cines", "educacion"]

# El cashback de las tarjetas de Cuscatlán sólo aplica en compras de $10 o más.
MIN_CUSCATLAN = 10

CATALOG = [
    {
        "bank": "Banco Agrícola",
        "products": [
            # --- crédito
            prod("Tarjeta Dorada Visa", "visa",
                 P("points", 1, PB, _AGRICOLA_DIAS, PB_VALUE)),
            prod("Tarjeta Dorada Mastercard", "mastercard",
                 P("points", 1, PB, _AGRICOLA_DIAS, PB_VALUE)),
            prod("Tarjeta Platinum Visa", "visa",
                 P("points", 1, PB, _AGRICOLA_DIAS, PB_VALUE)),
            prod("Tarjeta Platinum Mastercard", "mastercard",
                 P("points", 1, PB, _AGRICOLA_DIAS + [
                     ("viajes", 2), ("restaurantes", 2)], PB_VALUE)),
            prod("Tarjeta Black Mastercard", "mastercard",
                 P("points", 2, PB, [("viajes", 3)], PB_VALUE)),
            prod("Tarjeta Infinite Visa", "visa",
                 P("points", 2, PB, [("restaurantes", 3)], PB_VALUE)),
            prod("Tarjeta Clásica Visa", "visa", P("points", 1, PB, point_value=PB_VALUE)),
            prod("Tarjeta Sin Membresía Mastercard", "mastercard",
                 P("points", 0.5, PB, point_value=PB_VALUE)),
            prod("Tarjeta San Nicolás Mastercard", "mastercard",
                 P("points", 1, PB, point_value=PB_VALUE, merchants=[("Farmacias San Nicolás", 2)])),
            prod("Tarjeta Joven (crédito)", "other",
                 P("points", 0.5, PB, [(s, 3) for s in _JOVEN_3] + [(s, 2) for s in _JOVEN_2],
                   PB_VALUE)),
            prod("Tarjeta Platinum LifeMiles Visa", "visa",
                 P("points", 1, "LifeMiles", merchants=[("Avianca", 2)])),
            prod("Tarjeta Infinite LifeMiles Visa", "visa",
                 P("points", 1, "LifeMiles", merchants=[("Avianca", 3)])),
            # --- débito: 1 punto por cada $2
            prod("Débito Clásica Mastercard", "mastercard", P("points", 0.5, PB, point_value=PB_VALUE)),
            prod("Débito Black Mastercard", "mastercard",
                 P("points", 0.5, PB, [("viajes", 2)], PB_VALUE)),
            prod("Débito Mastercard", "mastercard", P("points", 0.5, PB, point_value=PB_VALUE)),
            prod("Débito Joven", "other",
                 P("points", 0.5, PB,
                   [(s, 2) for s in ("suscripciones", "gimnasio-deporte", "transporte", "gaming",
                                     "telefonia-internet", "educacion", "conciertos-eventos")]
                   + [(s, 1) for s in ("delivery", "compras-en-linea", "comida-rapida",
                                       "cines", "tecnologia")],
                   PB_VALUE)),
            prod("Débito Digital Preferencial", "other",
                 P("points", 0.5, PB, [("supermercado", 2, MON), ("gasolina", 2, WED),
                                       ("comida-rapida", 2, FRI)], PB_VALUE)),
        ],
    },
    {
        "bank": "Banco de América Central",
        "products": [
            # --- cashback
            prod("EconoMía Clásica", "visa",
                 P("cashback", 0.01, "SUPERMERCADOS", [("supermercado", 0.05), ("gasolina", 0.03)])),
            prod("EconoMía Dorada", "visa",
                 P("cashback", 0.01, "SUPERMERCADOS", [("supermercado", 0.05), ("gasolina", 0.03)])),
            prod("American Express Blue", "amex",
                 P("cashback", 0.01, "AMEX BLUE",
                   [(s, 0.05) for s in ("suscripciones", "viajes")]
                   + [(s, 0.03) for s in ("gasolina", "supermercado", "restaurantes", "comida-rapida")])),
            # --- millas
            prod("Millas Plus Visa Infinite", "visa", P("points", 2, "Millas Plus")),
            prod("Millas Plus Visa Platinum", "visa", P("points", 1.5, "Millas Plus")),
            prod("Millas Plus Visa Gold", "visa", P("points", 1.25, "Millas Plus")),
            prod("MillasPlus Mastercard Platino", "mastercard", P("points", 1.5, "Millas Plus")),
            prod("MillasPlus Mastercard Dorada", "mastercard", P("points", 1.25, "Millas Plus")),
            prod("lifemiles Visa Infinite", "visa", P("points", 2, "LifeMiles")),
            prod("lifemiles Visa Platinum", "visa", P("points", 1.5, "LifeMiles")),
            prod("lifemiles Visa Gold", "visa", P("points", 1.25, "LifeMiles")),
            prod("lifemiles Amex Gold", "amex", P("points", 1.25, "LifeMiles")),
            prod("lifemiles Amex Premium", "amex", P("points", 1.5, "LifeMiles")),
            prod("lifemiles Amex Elite", "amex", P("points", 2, "LifeMiles")),
            prod("AAdvantage Amex Gold", "amex", P("points", 1, "AAdvantage")),
            prod("AAdvantage Amex Platinum", "amex", P("points", 1, "AAdvantage")),
            prod("AAdvantage Mastercard Dorada", "mastercard", P("points", 1, "AAdvantage")),
            prod("AAdvantage Mastercard Platinum", "mastercard", P("points", 1, "AAdvantage")),
            # --- de un solo comercio
            prod("Selectos Mastercard Clásica", "mastercard",
                 P("cashback", 0, "Dólares Selectos", merchants=[("Súper Selectos", 0.07)])),
            prod("Selectos Mastercard Dorada", "mastercard",
                 P("cashback", 0, "Dólares Selectos", merchants=[("Súper Selectos", 0.07)])),
            prod("Selectos Mastercard Platino", "mastercard",
                 P("cashback", 0, "Dólares Selectos", merchants=[("Súper Selectos", 0.07)])),
            prod("PriceSmart Visa", "visa",
                 P("cashback", 0, "PriceCash", merchants=[("PriceSmart", 0.05)])),
            # --- puntos BAC: 1 por dólar
            prod("Mastercard Clásica", "mastercard", P("points", 1, "Puntos BAC")),
            prod("Mastercard Gold", "mastercard", P("points", 1, "Puntos BAC")),
            prod("Mastercard Platinum", "mastercard", P("points", 1, "Puntos BAC")),
            prod("Visa Clásica", "visa", P("points", 1, "Puntos BAC")),
            prod("Visa Gold", "visa", P("points", 1, "Puntos BAC")),
            prod("Visa Platinum", "visa", P("points", 1, "Puntos BAC")),
            prod("American Express Clásica", "amex", P("points", 1, "Puntos BAC")),
            prod("American Express Gold", "amex", P("points", 1, "Puntos BAC")),
            prod("Débito Visa Clásica", "visa", P("points", 1, "Puntos BAC")),
            prod("Débito Visa Platino", "visa", P("points", 1, "Puntos BAC")),
            prod("Débito Mastercard Clásica", "mastercard", P("points", 1, "Puntos BAC")),
        ],
    },
    {
        "bank": "Banco Cuscatlán",
        "products": [
            # Fuente: bancocuscatlan.com/tarjetas/de-credito (septiembre 2026). Los
            # bonos de bienvenida, Priority Pass, etc. no se modelan.
            # --- de un solo comercio
            # El 6 % es sólo "en gasolineras UNO y tiendas Pronto": se reconocen por el
            # nombre en la descripción. Sin él (o con otra gasolinera) no hay descuento.
            prod("UNO", "visa",
                 P("discount", 0, "Descuento UNO",
                   merchants=[("Gasolineras UNO", 0.06), ("Tiendas Pronto", 0.06)]),
                 P("points", 1, "MultiPuntos")),
            prod("UNO Oro", "visa",
                 P("discount", 0, "Descuento UNO",
                   merchants=[("Gasolineras UNO", 0.06), ("Tiendas Pronto", 0.06)]),
                 P("points", 1, "MultiPuntos")),
            prod("Selectos", "other",
                 P("discount", 0, "Descuento Selectos", merchants=[("Súper Selectos", 0.07)])),
            prod("Cash Back Tigo", "other",
                 P("cashback", 0, "Cash Back Tigo", merchants=[("Tigo", 0.20)], min_amount=MIN_CUSCATLAN)),
            # --- cashback
            prod("Cash Back Visa", "visa",
                 P("cashback", 0, "Cash Back", [("gasolina", 0.05), ("restaurantes", 0.05)],
                   min_amount=MIN_CUSCATLAN)),
            prod("PedidosYa", "visa",
                 P("cashback", 0, "Cashback PedidosYa",
                   [("delivery", 0.05), ("restaurantes", 0.05), ("comida-rapida", 0.05)],
                   min_amount=MIN_CUSCATLAN)),
            # El 5 % en servicios básicos sólo vale si el pago es un cargo automático
            # (Pagos Automáticos): el gasto lo marca así al registrarse.
            prod("ePay", "mastercard",
                 P("cashback", 0.01, "ePay",
                   [("suscripciones", 0.05), ("delivery", 0.05)]
                   + [(s, 0.05, ANY_DAY, True) for s in ("agua", "electricidad", "telefonia-internet")],
                   min_amount=MIN_CUSCATLAN)),
            prod("NIU", "visa",
                 P("cashback", 0, "Cashback NIU", [("restaurantes", 0.05)], min_amount=MIN_CUSCATLAN)),
            # --- MultiPuntos
            prod("MultiPuntos Visa Clásica", "visa", P("points", 1, "MultiPuntos")),
            prod("MultiPuntos Visa Oro", "visa", P("points", 1, "MultiPuntos")),
            prod("MultiPuntos Oro Mastercard", "mastercard", P("points", 1, "MultiPuntos")),
            prod("MultiPuntos Visa Platinum", "visa",
                 P("points", 1, "MultiPuntos",
                   [("restaurantes", 2), ("comida-rapida", 2), ("cines", 2),
                    ("conciertos-eventos", 2)])),
            prod("MultiPuntos Plus Oro", "other", P("points", 2, "MultiPuntos")),
            prod("Private Issue", "other", P("points", 2, "MultiPuntos")),
            prod("Signature", "visa",
                 P("points", 1, "MultiPuntos", [("viajes", 3), ("restaurantes", 2)])),
            # --- millas
            prod("lifemiles Infinite", "visa", P("points", 1, "LifeMiles")),
            prod("United Platinum", "other",
                 P("points", 1, "MileagePlus",
                   [("restaurantes", 2), ("viajes", 2), ("cines", 2), ("conciertos-eventos", 2)],
                   merchants=[("United Airlines", 2)])),
            prod("United Signature", "other",
                 P("points", 1, "MileagePlus",
                   [("restaurantes", 2), ("viajes", 2), ("cines", 2), ("conciertos-eventos", 2)],
                   merchants=[("United Airlines", 2)])),
        ],
    },
    {
        "bank": "Banco Atlántida",
        "products": [
            # Un plan de lealtad a la vez: el cliente elige uno. Se deja activo
            # sólo MegaPuntos Compras; los otros dos quedan cargados e inactivos
            # (si el titular elige otro, se cambia desde el admin).
            prod("One", "visa",
                 P("points", 3, "MegaPuntos Compras"),
                 P("cashback", 0.015, "Cashback One", active=False),
                 ),
        ],
    },
    {
        "bank": "Banco Promérica",
        "products": [
            prod("Visa PREMIA Infinite", "visa", P("points", 2, "Puntos Promerica")),
            prod("Visa PREMIA Signature", "visa", P("points", 2, "Puntos Promerica")),
            prod("Mastercard Black", "mastercard", P("points", 2, "Puntos Promerica")),
            prod("Visa PREMIA Platinum", "visa",
                 P("points", 1, "Puntos Promerica",
                   [("supermercado", 2), ("farmacia", 2), ("gasolina", 2)])),
            prod("Visa PREMIA Gold", "visa", P("points", 2, "Puntos Promerica")),
            prod("Visa PREMIA Clásica", "visa", P("points", 1, "Puntos Promerica")),
            prod("Mi Super Platinum", "visa",
                 P("cashback", 0, "Cashback Mi Super",
                   [("supermercado", 0.07), ("restaurantes", 0.02)])),
            prod("Mi Super Gold", "visa",
                 P("cashback", 0.01, "Cashback Mi Super",
                   [("supermercado", 0.07), ("restaurantes", 0.02)])),
            prod("Mi Super Clásica", "visa",
                 P("cashback", 0.01, "Cashback Mi Super",
                   [("supermercado", 0.07), ("restaurantes", 0.02)])),
            prod("Visa Mi Tarjeta", "visa", P("discount", 0.01, "Descuento Mi Tarjeta")),
            prod("Visa Mi Tarjeta Estudio", "visa",
                 P("discount", 0.01, "Descuento Mi Tarjeta Estudio", [("educacion", 0.05)])),
            prod("Visa Débito", "visa", P("points", 1, "Puntos Promerica")),
        ],
    },
    {
        "bank": "Banco Azul",
        "products": [
            prod("Tarjeta de Crédito Clásica", "visa",
                 P("points", 1, "Puntos Azul", [("supermercado", 2)], 0.005)),
            prod("Tarjeta de Crédito Oro", "visa",
                 P("points", 1.5, "Puntos Azul", [("supermercado", 2.5)], 0.005)),
            prod("Tarjeta de Crédito Platinum", "visa",
                 P("points", 2, "Puntos Azul", [("supermercado", 3), ("farmacia", 4)], 0.005)),
            prod("Tarjeta de Débito", "visa", P("points", 1, "Puntos Azul", point_value=0.005)),
        ],
    },
    {
        "bank": "Banco Hipotecario",
        "products": [
            prod("Visa Clásica Internacional", "visa", P("points", 1, "Puntos BH")),
            prod("Visa Oro", "visa", P("points", 1, "Puntos BH")),
            prod("Visa Platinum", "visa", P("points", 1, "Puntos BH")),
        ],
    },
    {
        "bank": "Banco Industrial El Salvador",
        "products": [
            prod("Visa Premium", "visa", P("points", 2, "Puntos Bi")),
        ],
    },
]
