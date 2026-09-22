# Economía por plan — cuánto le queda a cada plan

> Actualizado el 22 sep 2026 con la tarifa real de Wompi (ver abajo). Todas las cifras salen de
> `scripts/economia_por_plan.py`, con los precios sembrados por `seed_billing_plans` y los de
> Gemini de `apps/ai/pricing.py`. Para regenerar con otros supuestos:
>
> ```bash
> python scripts/economia_por_plan.py --fee-pct 5.5 --fee-fixed 0 --iva 0 --piso 5
> ```
>
> Complementa `COSTOS-Y-ESCALA.md` (infraestructura) y la sección de costos de IA de
> `moneyapp/docs/backlog-nuevas-funciones.md`.

## La tarifa de Wompi (confirmada 22-sep-2026)

**Sin cuota fija por cobro** — esto es lo que cambia todo respecto a la versión anterior de este
documento, que asumía 3.5% + $0.30 fijo (un supuesto de `COSTOS-Y-ESCALA.md`, no un dato). El
fijo era lo que más pesaba en los planes baratos ($0.99, $1.99); sin él, el costo de cobrar es
proporcional al precio y deja de castigar especialmente a Plus.

Composición confirmada: **3.5% de comisión de Wompi** + **2% de anticipo de IVA** sobre el monto
(mecanismo de percepción del IVA salvadoreño, 13%, para procesadores de tarjeta — un adelanto a
cuenta del propio IVA del comercio, no una ganancia de Wompi). Ejemplo dado: en un cobro de
**$1.99 la comisión total es de ~$0.10**.

**Cómo se modeló acá:** las tablas de abajo usan `--fee-pct 5.5 --fee-fixed 0 --iva 0` (3.5% +
2% sumados en línea recta, sin IVA adicional compuesto encima). Con eso, $1.99 × 5.5% = $0.109
(~$0.11) — un centavo por encima del ejemplo de $0.10 que dieron, dentro de lo esperable por
redondeo. Si la tarifa real compone distinto (p. ej. IVA del 13% sobre la comisión de Wompi
además del 2% de anticipo), hay que volver a correr el script con esos flags — la fórmula exacta
de `comision()` está comentada en `scripts/economia_por_plan.py`.

**Pendiente de confirmar:** si Wompi permite cobros recurrentes con tarjeta guardada — sigue sin
verificar (ver `ROADMAP.md` 1.1 y `apps/billing/providers.py`, `WompiProvider` es un esqueleto).
Sin eso, un plan mensual obliga a pagar a mano cada mes, y eso importa más que cualquier decimal
de la comisión.

## Lo que dicen los números

1. **Sin cuota fija, la comisión deja de castigar a los planes baratos.** Plus mensual ($0.99)
   entrega ahora ~5.5% de su precio al procesador (antes ~34% con el fijo asumido de $0.30); el
   neto pasa de $0.66 a $0.94.
2. **La IA sigue sin romper ningún plan por sí sola**, ni con un usuario intensivo y los precios
   de 2027. El margen más ajustado sigue siendo **Pro anual al techo de sus cuotas**: $0.52/mes
   hasta 2026, $0.18/mes desde 2027 (mismo problema que antes: el fijo ya no pesa, pero acá no
   hay fijo que restar).
3. **El lifetime ($19.99) sigue siendo la exposición real**, aunque menos grave que con el fijo
   asumido antes: con la cuota de Pro al techo, el pago único se agota en 1.6–2.4 años (antes
   también). Dándole la cuota de Plus, aguanta 5.6–8.8 años.
4. **Los costos fijos** siguen en el supuesto de ~$5/mes (Cloudflare Pages + capas gratis) — con
   eso alcanzan de 3 a 12 suscriptores para cubrirlos según plan y uso (tabla E).
5. **El 1-ene-2027 sigue subiendo el costo de la IA** (`gemini-3.8-flash` deja la promoción): el
   techo de Pro sube de ~$1.22 netos a ~$0.88 al mes.

**Supuestos:** comisión 5.5% sin fijo (3.5% Wompi + 2% anticipo IVA); infraestructura $0.01 por
usuario al mes; costos fijos $5 al mes.

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
| Plus mensual | $0.99 | $0.94 | $0.92 | $0.87 | $0.69 | $0.76 |
| Plus anual | $0.83 | $0.79 | $0.77 | $0.72 | $0.55 | $0.61 |
| Pro mensual | $1.99 | $1.88 | $1.86 | $1.81 | $1.64 | $1.22 |
| Pro anual | $1.25 | $1.18 | $1.16 | $1.11 | $0.94 | $0.52 |

### B. Lo que le queda a cada plan por suscriptor y mes, desde el 1-ene-2027 (Flash a precio completo)

| Plan | Ingreso/mes | Neto de comisión | Ligero | Medio | Intensivo | Techo del plan |
|---|---|---|---|---|---|---|
| Plus mensual | $0.99 | $0.94 | $0.91 | $0.83 | $0.56 | $0.65 |
| Plus anual | $0.83 | $0.79 | $0.76 | $0.68 | $0.41 | $0.51 |
| Pro mensual | $1.99 | $1.88 | $1.85 | $1.78 | $1.50 | $0.88 |
| Pro anual | $1.25 | $1.18 | $1.15 | $1.08 | $0.80 | $0.18 |

### C. Lifetime ($19.99 de pago único): ¿en cuánto tiempo deja de dejar plata?

Ingreso neto de comisión: $18.89. Meses hasta que el costo acumulado lo iguala:

| Perfil (con la cuota de Pro) | Hasta 31-dic-2026 | Desde 1-ene-2027 |
|---|---|---|
| Ligero | 923 meses (76.9 años) | 663 meses (55.2 años) |
| Medio | 284 meses (23.7 años) | 185 meses (15.4 años) |
| Intensivo | 78 meses (6.5 años) | 50 meses (4.2 años) |
| Techo Pro | 28 meses (2.4 años) | 19 meses (1.6 años) |
| Techo con la cuota de **Plus** (30 recibos, 50 textos, 20 chats) | 105 meses (8.8 años) | 67 meses (5.6 años) |
| Sin usar IA (sólo infraestructura) | 1889 meses (157 años) | — |

### D. Cuánto pesa la comisión: % del precio que se lleva el cobro

> Esta tabla es de sensibilidad (qué pasaría con distintos supuestos de fijo) y quedó de la
> versión anterior del documento — con la tarifa real confirmada (sin fijo), la fila que aplica
> es la de "fijo $0.00", que no está en la tabla; a 3.5–5.5% de comisión sin fijo, el % que se
> lleva el cobro es directamente ese mismo porcentaje (5.5% en Plus y Pro, mensual o anual: la
> comisión porcentual no distingue plazo).

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
| Plus mensual | 6 | 6 | 9 |
| Plus anual | 7 | 7 | 12 |
| Pro mensual | 3 | 3 | 3 |
| Pro anual | 4 | 5 | 6 |


## Lo que no se sabe, y cambia las cuentas

- **Si Wompi permite cobros recurrentes con tarjeta guardada.** Sigue sin verificar —
  `WompiProvider` es un esqueleto (sus cuatro métodos levantan `NotImplementedError`) y el diseño
  parte de un *enlace de pago hosteado*. Es más importante que cualquier decimal de la comisión:
  sin cobro recurrente, un plan mensual obliga al usuario a volver a pagar a mano cada mes.
- **Los costos fijos reales.** El piso de $5 al mes es un supuesto (Cloud Run, Neon, Cloudflare,
  Sentry y Mailgun en sus capas gratis, más el dominio). Hay que mirarlo contra la factura real
  de Google Cloud y de los demás servicios.
- **Los tokens de razonamiento** de los modelos 3.x, que se cobran como salida. No están en
  ninguna tabla; `AIUsage` los mostrará desde la primera llamada real.
- **La prueba gratis** (`Plan.trial_days`): un usuario en prueba gasta IA sin generar ingreso.
- Reembolsos y contracargos, impuestos propios y facturación, y el costo de atender soporte.

## Decisiones (revisadas 22-sep-2026 con la tarifa real)

- **Lifetime.** Con la tarifa real el problema se atenúa pero no desaparece: (a) dejarlo en
  $19.99 con la cuota de Pro sigue sin ser rentable para quien la usa al máximo (1.6–2.4 años).
  (b) Darle la cuota de Plus lo lleva a 5.6–8.8 años, ya razonable. (c) Subir el precio. (d)
  Limitar el chat, la operación que más pesa en la cuota de Pro. Sin cambios de este documento:
  sigue siendo una decisión abierta.
- **Empujar el anual.** Con comisión puramente porcentual (sin fijo), el anual ya no tiene la
  ventaja tan marcada que tenía por diluir un fijo — mensual y anual pagan el mismo % de
  comisión. Lo que sigue favoreciendo al anual es sólo el flujo de caja (cobrar una vez), no la
  comisión.
- **Precio de Plus mensual.** A $0.99 la comisión ahora es ~5.5% (antes ~34% con el fijo
  asumido) — deja de ser un problema por sí solo.
- **El tope de Pro.** Sigue siendo el que lleva el techo más alto (Pro anual, $0.18/mes en 2027).
  Es un número de `Plan.features`, editable desde el admin sin deploy.
