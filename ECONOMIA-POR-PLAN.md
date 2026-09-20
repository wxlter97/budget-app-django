# Economía por plan — cuánto le queda a cada plan

> Calculado el 20 sep 2026. Todas las cifras salen de `scripts/economia_por_plan.py`, con los
> precios sembrados por `seed_billing_plans` y los de Gemini de `apps/ai/pricing.py`. Para
> regenerar con otros supuestos (**la comisión de Wompi es la incógnita más grande**):
>
> ```bash
> python scripts/economia_por_plan.py --fee-pct 2.9 --fee-fixed 0.15 --iva 13 --piso 5
> ```
>
> Complementa `COSTOS-Y-ESCALA.md` (infraestructura) y la sección de costos de IA de
> `moneyapp/docs/backlog-nuevas-funciones.md`.

## Lo que dicen los números

1. **La comisión de cobro manda, no la IA.** Con un fijo de $0.30 por cobro, Plus mensual ($0.99)
   entrega un tercio de su precio al procesador; el plan anual, 6–7 %. Un usuario ligero o medio
   cuesta 1–9 ¢ de IA al mes.
2. **La IA no rompe ningún plan por sí sola**, ni con un usuario intensivo y los precios de 2027.
   El único con el margen fino es **Pro anual usado al techo de sus cuotas**: le quedan ~$0.18 al
   mes (de $1.25) desde el 1-ene-2027.
3. **El lifetime ($19.99) es la exposición real.** Con la cuota de Pro llevada al máximo el pago
   único se agota en **1.6–2.4 años**; con un usuario intensivo, en 4–7. Dándole al lifetime la
   cuota de Plus en vez de la de Pro, el techo aguanta 5.6–8.8 años (tabla C).
4. **Los costos fijos bajaron al mover el front a Cloudflare Pages.** Con ~$5 al mes de piso
   (supuesto, ver abajo) alcanzan de 3 a 18 suscriptores para cubrirlo, según plan y uso (tabla E).
   Con el piso de $25 que asumía `COSTOS-Y-ESCALA.md` (cuando el front estaba en Vercel Pro) serían
   unos cinco veces más.
5. **El 1-ene-2027 sube el costo.** `gemini-3.8-flash` deja de ser promocional y pasa a costar el
   doble: el techo de Pro sube de ~$0.66 a ~$0.99 al mes.

**Supuestos:** comisión 3.5% + $0.30 por cobro; infraestructura $0.01 por usuario al mes; costos fijos $5 al mes.

### A. Costo de IA por usuario al mes

| Perfil | Hasta 31-dic-2026 | Desde 1-ene-2027 |
|---|---|---|
| Ligero | $0.01 | $0.02 |
| Medio | $0.06 | $0.09 |
| Intensivo | $0.23 | $0.37 |
| Techo Plus | $0.17 | $0.27 |
| Techo Pro | $0.66 | $0.99 |

### B. Lo que le queda a cada plan por suscriptor y mes, hasta el 31-dic-2026 (precio promocional del Flash)

| Plan | Ingreso/mes | Neto de comisión | Ligero | Medio | Intensivo | Techo del plan |
|---|---|---|---|---|---|---|
| Plus mensual | $0.99 | $0.66 | $0.63 | $0.59 | $0.41 | $0.48 |
| Plus anual | $0.83 | $0.78 | $0.76 | $0.71 | $0.54 | $0.60 |
| Pro mensual | $1.99 | $1.62 | $1.60 | $1.55 | $1.38 | $0.96 |
| Pro anual | $1.25 | $1.18 | $1.16 | $1.11 | $0.94 | $0.52 |

### B. Lo que le queda a cada plan por suscriptor y mes, desde el 1-ene-2027 (Flash a precio completo)

| Plan | Ingreso/mes | Neto de comisión | Ligero | Medio | Intensivo | Techo del plan |
|---|---|---|---|---|---|---|
| Plus mensual | $0.99 | $0.66 | $0.63 | $0.55 | $0.28 | $0.37 |
| Plus anual | $0.83 | $0.78 | $0.75 | $0.68 | $0.40 | $0.50 |
| Pro mensual | $1.99 | $1.62 | $1.59 | $1.52 | $1.24 | $0.62 |
| Pro anual | $1.25 | $1.18 | $1.15 | $1.08 | $0.80 | $0.18 |

### C. Lifetime ($19.99 de pago único): ¿en cuánto tiempo deja de dejar plata?

Ingreso neto de comisión: $18.99. Meses hasta que el costo acumulado lo iguala:

| Perfil (con la cuota de Pro) | Hasta 31-dic-2026 | Desde 1-ene-2027 |
|---|---|---|
| Ligero | 928 meses (77.3 años) | 666 meses (55.5 años) |
| Medio | 285 meses (23.8 años) | 186 meses (15.5 años) |
| Intensivo | 79 meses (6.6 años) | 50 meses (4.2 años) |
| Techo Pro | 29 meses (2.4 años) | 19 meses (1.6 años) |
| Techo con la cuota de **Plus** (30 recibos, 50 textos, 20 chats) | 106 meses (8.8 años) | 68 meses (5.6 años) |
| Sin usar IA (sólo infraestructura) | 1899 meses (158 años) | — |

### D. Cuánto pesa la comisión: % del precio que se lleva el cobro

| Plan | % de comisión | fijo $0.10 | fijo $0.20 | fijo $0.30 | fijo $0.40 | fijo $0.50 |
|---|---|---|---|---|---|---|
| Plus mensual | 2.5% | 13% | 23% | 33% | 43% | 53% |
| Plus mensual | 3.5% | 14% | 24% | 34% | 44% | 54% |
| Plus mensual | 4.5% | 15% | 25% | 35% | 45% | 55% |
| Pro mensual | 2.5% | 8% | 13% | 18% | 23% | 28% |
| Pro mensual | 3.5% | 9% | 14% | 19% | 24% | 29% |
| Pro mensual | 4.5% | 10% | 15% | 20% | 25% | 30% |
| Plus anual | 2.5% | 4% | 5% | 6% | 7% | 8% |
| Plus anual | 3.5% | 5% | 6% | 7% | 8% | 9% |
| Plus anual | 4.5% | 6% | 7% | 8% | 9% | 10% |
| Pro anual | 2.5% | 3% | 4% | 5% | 5% | 6% |
| Pro anual | 3.5% | 4% | 5% | 6% | 6% | 7% |
| Pro anual | 4.5% | 5% | 6% | 7% | 7% | 8% |

### E. Suscriptores necesarios para cubrir $5 al mes de costos fijos

| Plan (con IA a precio 2027) | Ligero | Medio | Intensivo |
|---|---|---|---|
| Plus mensual | 8 | 9 | 18 |
| Plus anual | 7 | 7 | 12 |
| Pro mensual | 3 | 3 | 4 |
| Pro anual | 4 | 5 | 6 |


## Lo que no se sabe, y cambia las cuentas

- **La tarifa real de Wompi El Salvador.** No es pública: `wompi.sv/tarifas` sólo dice *sin cuota
  de manejo mensual*, que el depósito es diario y que retienen **IVA (13 %) sobre la comisión**.
  Los valores por defecto (3.5 % + $0.30) son los que implica `COSTOS-Y-ESCALA.md` (neto ~$0.65
  sobre $0.99), **no un dato**. Cómo obtenerla: pedirla al ejecutivo de Wompi o leerla en el
  panel/contrato del comercio. Con 2.9 % + $0.15 + IVA, el neto de Plus mensual pasa de $0.66 a
  $0.79.
- **Si Wompi permite cobros recurrentes con tarjeta guardada.** La página no lo menciona, y
  `WompiProvider` es un esqueleto **sin verificar**: sus cuatro métodos levantan
  `NotImplementedError` y el diseño parte de un *enlace de pago hosteado*. Si no hay cobro
  recurrente, un plan mensual obliga al usuario a volver a pagar a mano cada mes, y eso cambia
  qué planes tienen sentido. Es más importante que cualquier decimal de la comisión.
- **Los costos fijos reales.** El piso de $5 al mes es un supuesto (Cloud Run, Neon, Cloudflare,
  Sentry y Mailgun en sus capas gratis, más el dominio). Hay que mirarlo contra la factura real
  de Google Cloud y de los demás servicios.
- **Los tokens de razonamiento** de los modelos 3.x, que se cobran como salida. No están en
  ninguna tabla; `AIUsage` los mostrará desde la primera llamada real.
- **La prueba gratis** (`Plan.trial_days`): un usuario en prueba gasta IA sin generar ingreso.
- Reembolsos y contracargos, impuestos propios y facturación, y el costo de atender soporte.

## Decisiones abiertas

Estas opciones no están decididas; cada una dice qué le hace a los números.

- **Lifetime.** (a) Dejarlo en $19.99 con la cuota de Pro y aceptar que quien la use al máximo
  no es rentable. (b) Darle la cuota de Plus: pasa de 1.6–2.4 a 5.6–8.8 años en el peor caso.
  (c) Subir el precio. (d) Limitar el chat, que es la operación que más pesa en la cuota de Pro.
- **Empujar el anual.** El fijo pesa ~6 % en el anual contra ~34 % en Plus mensual.
- **Precio de Plus mensual.** A $0.99 la comisión fija se lleva ~34 %; a $1.49 se llevaría ~24 %.
  El script acepta cambiar `PLANES` para probar otros precios.
- **El tope de Pro.** Sus cuotas (100 recibos, 200 textos, 100 chats) son las que llevan el
  techo a $0.99 en 2027. Es un número de `Plan.features`, editable desde el admin sin deploy.
