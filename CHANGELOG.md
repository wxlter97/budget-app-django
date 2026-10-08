# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado manual (no automático): cada release que cambia comportamiento de
cara al usuario o a la API se bumpea a mano y se documenta acá, en el mismo
commit/PR que trae el cambio. La versión vive en
`config/settings.py` (`SPECTACULAR_SETTINGS["VERSION"]`) y es independiente
de la del frontend (`moneyapp`, que tiene su propio changelog).

Para "qué falta"/planeamiento ver `ROADMAP.md`. Este archivo es sólo lo que
ya se envió.

## [1.10.0] - 2026-10-08

### Agregado
- `POST /ai/statement/`: lee un estado de cuenta (PDF o foto, hasta 12 MB) y
  devuelve una **candidata** editable -- datos de la cartera (banco, últimos 4,
  límite, día de corte y de pago, tasa, saldo al corte) y los movimientos del
  período, con categoría sugerida por historial y duplicados marcados contra la
  cartera indicada. No crea nada; mismo contrato de confianza por campo que los
  recibos. Comparte la cuota mensual `ai_receipts_per_month` con los recibos
  (nueva operación `statement` en `AIUsage`, migración `ai.0002`).

## [1.9.0] - 2026-10-06

### Agregado
- `Workspace.week_start_day` (0 = lunes … 6 = domingo, default 0): día en que
  arranca la semana del presupuesto semanal. Editable por el dueño; mover la
  grilla reinicia el cierre de provisión, igual que cambiar `budget_period`.
  `periods.period_start`/`previous_period_start` reciben `week_start`.
  Migración `workspaces.0007`.

## [1.8.0] - 2026-10-02

### Agregado
- Provisión acumulada apagable: `Category.rollover_surplus` (por categoría) y
  `Workspace.rollover_surplus` (interruptor global, solo owner; manda sobre el
  de la categoría). Apagado, el cierre de período no acumula y el reporte de
  presupuesto y los avisos de umbral ignoran lo ya acumulado, que se conserva.
  Ambos van por defecto encendidos y entran en el respaldo del workspace.
- `POST /categories/{id}/reset-provision/` y
  `POST /workspaces/{id}/reset-provisions/` (owner): ponen en cero lo
  acumulado, de una categoría o de todo el workspace.
- El aviso de resumen mensual trae `data.month` (`YYYY-MM`, el mes del que
  habla) para que el cliente abra ese mes.

### Cambiado
- Resumen mensual: una línea por idea (con "• ") en vez de un párrafo corrido,
  tanto el texto de Gemini como el de respaldo sin IA.

## [1.7.0] - 2026-09-22

Antes de este archivo no hubo changelog formal -- el historial vive en
`ROADMAP.md` (con fechas) y en los mensajes de commit. Esta primera entrada
consolida lo más reciente como punto de partida.

### Agregado
- Resumen mensual por IA (`apps.ai.summary`), notificación opcional
  (`warn_monthly_summary`).
- Dictado de transacciones por voz (`POST /ai/voice/`), mismo contrato y
  cuota que el texto libre.
- Chat de finanzas (`POST /ai/chat/`): responde preguntas sobre reportes ya
  calculados, nunca inventa datos ni toca la base directo.
- Documentación técnica interna en `/docs/` (staff-only) y este changelog.

### Corregido
- Registrar a mano (desde "Programado") una ocurrencia de un gasto
  recurrente un día antes de que corriera el job automático la duplicaba al
  día siguiente -- el alta manual no avanzaba `next_due_date` de la regla.

### Documentación
- Corregidas referencias desactualizadas en `ROADMAP.md`/`ECONOMIA-POR-PLAN.md`
  que describían `WompiProvider` como un esqueleto sin implementar (ya
  estaba completo) y `GS_BUCKET_NAME` como pendiente (ya estaba verificado).
- Cerrada la decisión de precios con la tarifa real de Wompi (3.5% comisión +
  2% IVA anticipo, sin cargo fijo).

## [1.6.0] y anteriores

Sin changelog formal. Ver `ROADMAP.md` y el historial de git.
