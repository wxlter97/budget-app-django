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
   recurso, no el primero.
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

## 8. Contactos / accesos que vas a necesitar en el momento

Completar antes de que haga falta, no durante el incidente:
- Acceso a GCP (`gcloud auth login` con la cuenta dueña del proyecto).
- Acceso a Neon, Vercel, Wompi, Mailgun, Sentry (si ya existe).
- Este archivo y `DEPLOY.md` en un lugar que se pueda abrir sin depender
  del propio `budget-api` estando caído (no confiar en que el repo esté
  siempre a mano si Cloud Run/GitHub tienen un corte simultáneo -- tenerlo
  clonado localmente alcanza).
