# Runbook — qué hacer cuando algo se rompe

Complementa `DEPLOY.md` (que cubre el camino feliz de instalar/deployar).
Esto es para cuando ya está en producción y algo anda mal. Servicio:
`budget-api` (Cloud Run, `us-east1`) + Neon (Postgres) + Vercel (frontend web).

---

## 1. El deploy que acabás de hacer rompió producción

**Sintoma**: `https://budget-api-XXXX.a.run.app/healthz/` no responde, o
responde pero con 500 en todo, justo después de un deploy.

1. **Revertir Cloud Run a la revisión anterior** (segundos, sin rebuild):
   ```bash
   # Ver las últimas revisiones y cuál sirve tráfico
   gcloud run revisions list --service budget-api --region us-east1

   # Mandar el 100% del tráfico a la revisión anterior (la que andaba bien)
   gcloud run services update-traffic budget-api --region us-east1 \
     --to-revisions REVISION_ANTERIOR=100
   ```
2. Confirmá que volvió: `curl https://budget-api-XXXX.a.run.app/healthz/`.
3. Recién ahí investigá el problema con calma (logs del deploy roto, ver §4)
   y arreglalo en una rama antes de reintentar.

Si el problema es una **migración** que ya corrió contra la base real (no
sólo código nuevo): revertir el tráfico de Cloud Run NO deshace la
migración. Ver §3.

## 2. El frontend (Vercel) rompió

- **Dashboard → Deployments** → buscá el último deploy que andaba bien →
  **"..." → Promote to Production**. Instantáneo, sin rebuild.
- CLI: `vercel rollback` desde el proyecto (`npx vercel rollback` si no está
  instalado global).

## 3. Una migración rompió la base de datos

No hay revert automático de migraciones en producción -- opciones, de menos
a más drástica:

1. **Si la migración es reversible** (la mayoría de los `AddField`/
   `CreateModel` lo son): `python manage.py migrate app_name NNNN_anterior`
   corriendo un Cloud Run Job puntual (mismo patrón que `budget-admin` en
   `DEPLOY.md` §2.3, cambiando el `--args`).
2. **Si no es reversible** (borró una columna con datos, por ejemplo):
   restaurar Neon a un punto en el tiempo anterior a la migración --
   Neon dashboard → **Branches → Restore** (plan free retiene ~24h). Esto
   pierde cualquier dato escrito después de ese punto, así que es el último
   recurso, no el primero. Si ya pasaron más de 24 h, el camino es el
   volcado diario a GCS: **§9**.
3. Siempre: revertí el código (Cloud Run, §1) ANTES de tocar la base, para
   que no siga escribiendo contra un esquema que ya no coincide.

## 4. Ver qué pasó (logs)

```bash
# Logs del servicio (arranque, requests, excepciones no manejadas)
gcloud run services logs read budget-api --region us-east1 --limit 100

# Sólo errores
gcloud run services logs read budget-api --region us-east1 --limit 100 \
  --log-filter "severity>=ERROR"

# Logs de un Cloud Run Job puntual (budget-cron, budget-admin, budget-migrate)
gcloud run jobs executions list --job budget-cron --region us-east1
gcloud run jobs executions logs read EXECUTION_ID --region us-east1
```

Si `SENTRY_DSN` está configurado (ver `.env.example`), los errores no
manejados también aparecen en Sentry con el stack trace completo -- más
rápido que buscarlos en los logs de Cloud Run.

## 5. El Cloud Scheduler dejó de disparar los recordatorios/recurrentes

```bash
gcloud scheduler jobs describe budget-cron-daily --location us-east1   # ¿está ENABLED?
gcloud scheduler jobs run budget-cron-daily --location us-east1        # forzar una corrida ya
gcloud run jobs executions list --job budget-cron --region us-east1    # ¿corrió? ¿falló?
```

Si `run` falla con un error de permisos, revisar que la service account
`budget-cron-invoker` todavía tenga `roles/run.invoker` sobre el job (ver
`DEPLOY.md` §6.2) -- se puede perder si alguien recreó el job con
`gcloud run jobs deploy` en vez de `update` (deploy no preserva IAM bindings
en todos los casos).

## 6. Wompi dejó de mandar webhooks (nadie se activa después de pagar)

1. Confirmar que el endpoint responde: `POST /api/v1/billing/webhooks/wompi/`
   -- un 501 significa que `WompiProvider` todavía no está implementado (ver
   `apps/billing/providers.py`), no un problema de infraestructura.
2. Si ya está implementado: revisar en el dashboard de Wompi el log de
   intentos de webhook (reintenta unas horas antes de darse por vencido).
3. Mientras se resuelve, dar el alta a mano: Django admin → Billing →
   Subscriptions → crear una con `provider=manual`, `status=active` (mismo
   mecanismo que usa `grandfather_existing_users`).

## 7. Sospecha de brecha de datos / acceso no autorizado

Sin un plan formal todavía (pendiente la revisión legal, ver checklist de
producción) -- mínimo indispensable mientras tanto:

1. Rotar `DJANGO_SECRET_KEY` (invalida todas las sesiones JWT activas) y
   cualquier credencial que se sospeche comprometida (Wompi, Mailgun,
   Secret Manager).
2. Revisar `gcloud run services logs read` alrededor de la ventana sospechosa.
3. Si hay evidencia real de acceso a datos de usuarios, van a necesitar
   saberlo -- aunque no haya todavía un proceso formal de notificación,
   escribirles es lo mínimo correcto (y probablemente legalmente exigido
   según cómo termine la ley de protección de datos de El Salvador).

## 8. Ventana de mantenimiento de emergencia

Para bajar el API a propósito unos minutos (p. ej. antes de una migración
riesgosa o una restauración de Neon): variable de entorno `MAINTENANCE_MODE`
(ver `apps/common/middleware.py`), no un flag en base de datos ni un botón
en el admin -- así sigue funcionando aunque la base esté justo en el medio
del problema.

1. En Cloud Run: `gcloud run services update budget-api --region us-east1
   --update-env-vars MAINTENANCE_MODE=True` (redeploy casi instantáneo, sin
   rebuild). Todo el API responde 503 con el mensaje de `MAINTENANCE_MESSAGE`,
   salvo `/healthz/` (Cloud Run sigue viendo el servicio sano) y `/admin/`
   (para poder seguir operando).
2. Hacer el trabajo riesgoso.
3. Quitarlo: `gcloud run services update budget-api --region us-east1
   --update-env-vars MAINTENANCE_MODE=False`.

El frontend (moneyapp) reconoce la respuesta 503 y muestra una pantalla de
"en mantenimiento" en vez del error genérico -- no hace falta avisarle nada
aparte.

## 9. Restaurar la base desde un backup

El job diario (`run_daily_tasks`) termina con `manage.py backup_database`, que
sube un volcado de `pg_dump --format=custom` a
`gs://$GS_BUCKET_NAME/backups/db/budget-AAAAMMDD-HHMMSS.dump` y borra los de
más de `DB_BACKUP_RETENTION_DAYS` días (30 por defecto, y nunca el último que
queda). Es lo que cubre la ventana más allá de las ~24 h de historial de Neon.

Antes que nada: **si el problema pasó hace menos de 24 h, restaurar la rama en
Neon es más rápido y no pierde nada** (§3.2). Lo de acá es para cuando eso ya
no alcanza.

```bash
# 1. Qué backups hay
gsutil ls -l gs://$GS_BUCKET_NAME/backups/db/

# 2. Bajar el que corresponde
gsutil cp gs://$GS_BUCKET_NAME/backups/db/budget-AAAAMMDD-HHMMSS.dump .

# 3. Ver qué trae, sin restaurar nada todavía
pg_restore --list budget-AAAAMMDD-HHMMSS.dump | head -40
```

**Restaurar nunca se hace encima de la base viva.** El orden es: crear una
rama nueva en Neon (Branches → New branch, sale gratis y tarda segundos),
restaurar ahí, mirar que los datos estén, y recién entonces apuntar
`DATABASE_URL` a esa rama.

```bash
# 4. Restaurar sobre la rama nueva (URL del branch, no la de producción)
pg_restore --no-owner --no-privileges --clean --if-exists \
  --dbname "postgresql://...rama-nueva.../budget?sslmode=require" \
  budget-AAAAMMDD-HHMMSS.dump

# 5. Verificar antes de mover a nadie
psql "postgresql://...rama-nueva.../budget?sslmode=require" \
  -c "select count(*) from transactions_transaction;"
```

6. Poner `MAINTENANCE_MODE=True` (§8), cambiar `DATABASE_URL` del servicio a
   la rama restaurada, sacar el mantenimiento.

Detalles que muerden:
- El volcado se hace con `--no-owner --no-privileges` porque el rol de Neon no
  es el mismo entre ramas; restaurar **sin** esas banderas llena la salida de
  errores de "role does not exist".
- `--clean --if-exists` es lo que permite restaurar sobre una base que ya tiene
  el esquema. Sobre una rama recién creada desde producción, eso es lo normal.
- El volcado es de la noche anterior: **se pierde lo escrito desde entonces.**
  Es el precio de llegar tarde, y la razón de que Neon sea siempre el primer
  intento.
- Si `pg_restore` se queja de versión, es el mismo problema que el de
  `pg_dump`: el cliente tiene que ser >= el servidor (ver Dockerfile).

## 10. Contactos / accesos que vas a necesitar en el momento

Completar antes de que haga falta, no durante el incidente:
- Acceso a GCP (`gcloud auth login` con la cuenta dueña del proyecto).
- Acceso a Neon, Vercel, Wompi, Mailgun, Sentry (si ya existe).
- Este archivo y `DEPLOY.md` en un lugar que se pueda abrir sin depender
  del propio `budget-api` estando caído (no confiar en que el repo esté
  siempre a mano si Cloud Run/GitHub tienen un corte simultáneo -- tenerlo
  clonado localmente alcanza).
