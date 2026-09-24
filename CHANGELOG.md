# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado manual (no automático): cada release que cambia comportamiento de
cara al usuario o a la API se bumpea a mano y se documenta acá, en el mismo
commit/PR que trae el cambio. La versión vive en
`config/settings.py` (`SPECTACULAR_SETTINGS["VERSION"]`) y es independiente
de la del frontend (`moneyapp`, que tiene su propio changelog).

Para "qué falta"/planeamiento ver `ROADMAP.md`. Este archivo es sólo lo que
ya se envió.

## [1.9.0] - 2026-09-23

### Agregado
- `GET /reports/members/`: gasto del mes por miembro (quién pagó, si la
  transacción se dividió entre personas; si no, quién la cargó).
- `GET /reports/can-afford/?amount=&category=`: "¿me alcanza?", cómo quedan
  la categoría y el presupuesto del período descontando lo programado
  (recurrentes y cuotas). Sin presupuesto, usa el flujo del mes.
- `GET /wallets/{id}/contributions/`: aportes y retiros por miembro en una
  cartera de ahorro (metas compartidas).
- Aviso de corte de tarjeta dos días antes (`warn_statement_cutoff`) y
  resumen semanal los lunes (`warn_weekly_summary`), con sus preferencias.
- `display_name` en las membresías.

### Cambiado
- La detección de recurrentes agrupa también por comercio o descripción:
  dos suscripciones en la misma categoría y tarjeta (Netflix y Spotify) ya
  no se anulan entre sí. Las sugerencias traen `name`, y un recurrente ya
  creado sólo oculta la sugerencia con un monto parecido.
- El aviso de estado de cuenta dice cuánto pagar para no pagar intereses.

## [1.8.0] - 2026-09-23

### Agregado
- `POST /memberships/leave/`: cualquier miembro puede salir de un presupuesto.
  El último dueño no puede (primero nombra a otro, o borra el presupuesto si
  está solo), y nadie puede dejar su único presupuesto.
- `/workspace-invitations/`: invitaciones pendientes del presupuesto activo.
  Cualquier miembro las ve; el dueño las cancela (`DELETE`, el enlace deja de
  funcionar) o reenvía el correo (`POST …/resend/`, como mucho uno por minuto).
- El job diario avisa por Discord (`OPS_WEBHOOK_URL`, por defecto el webhook
  de soporte) qué pasos fallaron.

### Cambiado
- `run_daily_tasks` ya no se corta en el primer error: los demás pasos corren
  igual (los cierres se saltan si fallaron los recurrentes), y al final sale
  con error para que Cloud Run marque la ejecución como fallida.

### Corregido
- Volver a sumar a alguien que se había ido o al que habían quitado (directo
  o aceptando una invitación) daba un 500: la membresía vieja, borrada con
  soft delete, seguía ocupando la restricción única. Ahora se revive.

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
