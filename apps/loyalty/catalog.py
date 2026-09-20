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
- beneficios de un solo comercio (Walmart, Súper Selectos, PriceSmart, Tigo,
  Farmacias San Nicolás, millas al comprar en Avianca): el modelo da tasas por
  rubro, y aplicarlas a todo el rubro sería mentir;
- tarjetas cuyo sitio sólo dice "hasta N" o no publica la tasa;
- Cuscatlán MultiPuntos, Davivienda por producto, Industrial (salvo Premium),
  ABANK y Apoyo Integral: sin datos verificables en su sitio.
"""

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

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

# (banco, nombre anterior, nombre nuevo): productos que ya estaban cargados a mano
# con otro nombre. Se renombran (no se crean de nuevo) para no perder las
# tarjetas de los usuarios que ya los tienen asignados.
RENAMES = [
    ("Banco Agrícola", "Tarjeta Oro", "Tarjeta Dorada Visa"),
    ("Banco de América Central", "CASHBACK BLUE", "American Express Blue"),
    ("Banco de América Central", "Economía", "EconoMía Clásica"),
]


def P(kind, default, name="", rates=(), point_value=None, active=True):
    return {
        "kind": kind, "name": name, "default": default, "rates": list(rates),
        "point_value": point_value, "active": active,
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
                 P("points", 1, PB, point_value=PB_VALUE)),
            prod("Tarjeta Joven (crédito)", "other",
                 P("points", 0.5, PB, [(s, 3) for s in _JOVEN_3] + [(s, 2) for s in _JOVEN_2],
                   PB_VALUE)),
            prod("Tarjeta Platinum LifeMiles Visa", "visa", P("points", 1, "LifeMiles")),
            prod("Tarjeta Infinite LifeMiles Visa", "visa", P("points", 1, "LifeMiles")),
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
            prod("UNO", "visa", P("discount", 0.06, "Descuento UNO")),
            prod("ePay", "mastercard", P("cashback", 0.01, "ePay")),
            prod("NIU", "visa"),
            prod("PedidosYa", "visa",
                 P("cashback", 0, "Cashback PedidosYa",
                   [("delivery", 0.05), ("restaurantes", 0.05), ("comida-rapida", 0.05)])),
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
