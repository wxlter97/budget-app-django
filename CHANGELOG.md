# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado manual (no automático): cada release que cambia comportamiento de
cara al usuario o a la API se bumpea a mano y se documenta acá, en el mismo
commit/PR que trae el cambio. La versión vive en
`config/settings.py` (`SPECTACULAR_SETTINGS["VERSION"]`) y es independiente
de la del frontend (`moneyapp`, que tiene su propio changelog).

Para "qué falta"/planeamiento ver `ROADMAP.md`. Este archivo es sólo lo que
ya se envió.

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
