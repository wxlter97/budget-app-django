# Documentación técnica

Referencia interna de cómo funciona el backend: propósito de cada app,
modelos principales, endpoints y reglas de negocio no obvias. Vive en
Markdown plano en `docs/reference/` y se sirve en `/docs/` (staff-only,
mismo login que `/admin/`).

**Se actualiza con cada cambio de comportamiento**: si un PR cambia una
regla de negocio, agrega/quita un endpoint, o cambia un modelo de forma
significativa, el `.md` de esa app se actualiza en el mismo PR. No es
documentación aparte que se pueda dejar para después: si queda desactualizada
deja de servir.

Para el historial de qué cambió y cuándo (versión por versión), ver el
[Changelog](/docs/changelog/).

Para roadmap/planeamiento (qué falta, qué se decidió diferir), ver
`ROADMAP.md` en la raíz del repo -- eso no se expone acá porque cambia de
forma muy distinta (es un plan, no una referencia de lo que ya existe).

## Cómo está organizado cada doc de app

Cada página en el menú "Apps" sigue más o menos esta forma:

- **Propósito**: qué problema resuelve esta app, en una o dos frases.
- **Modelos principales**: los que importa conocer para entender el dominio,
  no un volcado de cada campo.
- **Endpoints**: la superficie de la API que expone, agrupada por lo que
  hace cada grupo (no una lista de rutas sin contexto).
- **Reglas de negocio / decisiones no obvias**: la parte que realmente vale
  la pena documentar -- el código ya dice el "qué", esto explica el "por
  qué" cuando no es evidente.
