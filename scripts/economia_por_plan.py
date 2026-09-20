#!/usr/bin/env python3
"""Economía por plan: cuánto le queda a cada plan después de la comisión de cobro,
la IA y la infraestructura. Imprime tablas en Markdown.

    python scripts/economia_por_plan.py
    python scripts/economia_por_plan.py --fee-pct 2.9 --fee-fixed 0.15 --iva 13 --piso 5

Todo lo que se puede discutir es un parámetro o una constante de arriba. La
**comisión de Wompi El Salvador no es pública** (wompi.sv/tarifas sólo dice "sin
cuota de manejo" y que retienen IVA sobre la comisión): los valores por defecto son
los que implica `COSTOS-Y-ESCALA.md` (neto ~$0.65 sobre $0.99), no un dato. Cuando
se conozca la tarifa real, se pasa por argumentos y se regenera todo.

No incluye: tokens de razonamiento de los modelos 3.x (se cobran como salida),
prueba gratis, reembolsos ni contracargos, impuestos propios ni el costo de soporte.
"""
import argparse

# --- Precios de los planes (`seed_billing_plans.py`), USD ----------------------------
# nombre: (precio, meses que cubre el cobro)
PLANES = {
    "Plus mensual": (0.99, 1),
    "Plus anual": (9.99, 12),
    "Pro mensual": (1.99, 1),
    "Pro anual": (14.99, 12),
}
LIFETIME = 19.99  # Pro, pago único

# --- Precios de Gemini, USD por millón de tokens (entrada, salida). apps/ai/pricing.py
LITE = (0.30, 2.50)  # gemini-3.5-flash-lite: texto libre y chat
FLASH_PROMO = (0.75, 3.75)  # gemini-3.8-flash hasta el 31-dic-2026
FLASH_2027 = (1.50, 7.50)  # gemini-3.8-flash desde el 1-ene-2027

# Tokens por operación (entrada, salida): los del backlog de moneyapp.
TOKENS = {
    "parseo": (630, 120),
    "dictado": (800, 120),
    "recibo": (2250, 300),  # punto medio de 1.5-3k
    "recibo_max": (3000, 300),
    "chat": (4000, 400),
    "resumen": (1200, 400),
}
# Qué modelo atiende cada operación: "lite" o "flash".
MODELO = {"parseo": "lite", "dictado": "flash", "recibo": "flash", "recibo_max": "flash",
          "chat": "lite", "resumen": "flash"}

# Uso mensual por perfil: {operación: cantidad}.
PERFILES = {
    "Ligero": {"parseo": 5, "recibo": 2, "resumen": 1},
    "Medio": {"parseo": 20, "dictado": 5, "recibo": 10, "chat": 5, "resumen": 1},
    "Intensivo": {"parseo": 60, "dictado": 20, "recibo": 40, "chat": 30, "resumen": 1},
}
# Techo de cada plan (cuotas de `seed_billing_plans`), con recibos de 3 000 tokens.
TECHO = {
    "Plus": {"recibo_max": 30, "parseo": 50, "chat": 20},
    "Pro": {"recibo_max": 100, "parseo": 200, "chat": 100},
}

INFRA_POR_USUARIO = 0.01  # USD/mes, COSTOS-Y-ESCALA.md ("Marginal total ~1 ¢")


def costo_ia(uso, flash):
    total = 0.0
    for op, n in uso.items():
        precio = LITE if MODELO[op] == "lite" else flash
        i, o = TOKENS[op]
        total += n * (i * precio[0] + o * precio[1]) / 1e6
    return total


def comision(precio, pct, fijo, iva):
    """Comisión por cobro: (porcentaje × precio + fijo) más IVA sobre la comisión."""
    return (pct / 100 * precio + fijo) * (1 + iva / 100)


def tabla(cabecera, filas):
    out = ["| " + " | ".join(cabecera) + " |", "|" + "---|" * len(cabecera)]
    out += ["| " + " | ".join(f) + " |" for f in filas]
    return "\n".join(out)


def usd(x):
    return f"-${abs(x):.2f}" if x < 0 else f"${x:.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fee-pct", type=float, default=3.5, help="comisión %% (defecto: 3.5)")
    ap.add_argument("--fee-fixed", type=float, default=0.30, help="fijo por cobro USD (0.30)")
    ap.add_argument("--iva", type=float, default=0.0, help="IVA %% sobre la comisión (0)")
    ap.add_argument("--piso", type=float, default=5.0, help="costos fijos al mes USD (5)")
    a = ap.parse_args()

    print(f"**Supuestos:** comisión {a.fee_pct}% + ${a.fee_fixed:.2f} por cobro"
          f"{f' + {a.iva}% de IVA sobre la comisión' if a.iva else ''}; infraestructura "
          f"${INFRA_POR_USUARIO:.2f} por usuario al mes; costos fijos ${a.piso:.0f} al mes.\n")

    ia = {}  # (perfil, época) -> costo mensual
    for etapa, flash in (("promo", FLASH_PROMO), ("2027", FLASH_2027)):
        for nombre, uso in PERFILES.items():
            ia[(nombre, etapa)] = costo_ia(uso, flash)
        for plan, uso in TECHO.items():
            ia[(f"Techo {plan}", etapa)] = costo_ia(uso, flash)

    # ---- A. Costo de IA por perfil -------------------------------------------------
    print("### A. Costo de IA por usuario al mes\n")
    perfiles = list(PERFILES) + ["Techo Plus", "Techo Pro"]
    print(tabla(["Perfil", "Hasta 31-dic-2026", "Desde 1-ene-2027"],
                [[p, usd(ia[(p, 'promo')]), usd(ia[(p, '2027')])] for p in perfiles]))

    # ---- B. Contribución por suscriptor y mes --------------------------------------
    for etapa, titulo in (("promo", "hasta el 31-dic-2026 (precio promocional del Flash)"),
                          ("2027", "desde el 1-ene-2027 (Flash a precio completo)")):
        print(f"\n### B. Lo que le queda a cada plan por suscriptor y mes, {titulo}\n")
        filas = []
        for plan, (precio, meses) in PLANES.items():
            base = plan.split()[0]
            neto = (precio - comision(precio, a.fee_pct, a.fee_fixed, a.iva)) / meses
            fila = [plan, usd(precio / meses), usd(neto)]
            for perfil in list(PERFILES) + [f"Techo {base}"]:
                fila.append(usd(neto - ia[(perfil, etapa)] - INFRA_POR_USUARIO))
            filas.append(fila)
        print(tabla(["Plan", "Ingreso/mes", "Neto de comisión", "Ligero", "Medio", "Intensivo",
                     "Techo del plan"], filas))

    # ---- C. Lifetime ---------------------------------------------------------------
    print("\n### C. Lifetime ($19.99 de pago único): ¿en cuánto tiempo deja de dejar plata?\n")
    neto_lt = LIFETIME - comision(LIFETIME, a.fee_pct, a.fee_fixed, a.iva)
    print(f"Ingreso neto de comisión: {usd(neto_lt)}. Meses hasta que el costo acumulado lo iguala:\n")
    filas = []
    for perfil in list(PERFILES) + ["Techo Pro"]:
        fila = [perfil]
        for etapa in ("promo", "2027"):
            m = neto_lt / (ia[(perfil, etapa)] + INFRA_POR_USUARIO)
            fila.append(f"{m:.0f} meses ({m/12:.1f} años)")
        filas.append(fila)
    # Alternativa a discutir: que el lifetime lleve la cuota de IA de Plus y no la de Pro.
    fila = ["Techo con la cuota de **Plus** (30 recibos, 50 textos, 20 chats)"]
    for etapa in ("promo", "2027"):
        m = neto_lt / (ia[("Techo Plus", etapa)] + INFRA_POR_USUARIO)
        fila.append(f"{m:.0f} meses ({m/12:.1f} años)")
    filas.append(fila)
    m0 = neto_lt / INFRA_POR_USUARIO
    filas.append(["Sin usar IA (sólo infraestructura)", f"{m0:.0f} meses ({m0/12:.0f} años)", "—"])
    print(tabla(["Perfil (con la cuota de Pro)", "Hasta 31-dic-2026", "Desde 1-ene-2027"], filas))

    # ---- D. Sensibilidad a la comisión --------------------------------------------
    print("\n### D. Cuánto pesa la comisión: % del precio que se lleva el cobro\n")
    fijos = [0.10, 0.20, 0.30, 0.40, 0.50]
    pcts = [2.5, 3.5, 4.5]
    filas = []
    for plan in ("Plus mensual", "Pro mensual", "Plus anual", "Pro anual"):
        precio = PLANES[plan][0]
        for pct in pcts:
            filas.append([plan, f"{pct}%"] + [
                f"{comision(precio, pct, f, a.iva) / precio * 100:.0f}%" for f in fijos])
    print(tabla(["Plan", "% de comisión"] + [f"fijo ${f:.2f}" for f in fijos], filas))

    # ---- E. Punto de equilibrio de los costos fijos --------------------------------
    print(f"\n### E. Suscriptores necesarios para cubrir ${a.piso:.0f} al mes de costos fijos\n")
    filas = []
    for plan, (precio, meses) in PLANES.items():
        neto = (precio - comision(precio, a.fee_pct, a.fee_fixed, a.iva)) / meses
        fila = [plan]
        for perfil in ("Ligero", "Medio", "Intensivo"):
            c = neto - ia[(perfil, "2027")] - INFRA_POR_USUARIO
            fila.append(f"{a.piso / c:.0f}" if c > 0 else "nunca")
        filas.append(fila)
    print(tabla(["Plan (con IA a precio 2027)", "Ligero", "Medio", "Intensivo"], filas))


if __name__ == "__main__":
    main()
