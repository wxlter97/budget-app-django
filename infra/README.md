# infra/

Configuración de infraestructura que conviene tener versionada en vez de
pegada a mano en una consola. Los pasos que la aplican están en `DEPLOY.md`.

## `gcs-lifecycle.json`

Reglas de ciclo de vida del bucket de recibos y backups (`DEPLOY.md` §2.5):

```bash
gcloud storage buckets update gs://TU-BUCKET --lifecycle-file=infra/gcs-lifecycle.json
```

Tiene **una sola regla**: abortar a las 24 h las subidas multiparte que
quedaron a medias. Una subida cortada (se cayó la red del teléfono con la foto
del recibo a medio subir) deja partes que se facturan como almacenamiento y que
nada vuelve a mirar nunca.

Lo que **no** hay acá, a propósito:

- **Reglas de cambio de clase** (`SetStorageClass`). De eso se encarga
  Autoclass, que además no cobra recuperación ni penaliza el borrado
  temprano — ver el porqué en `DEPLOY.md` §2.5. Autoclass y las reglas de
  clase son incompatibles: si se agrega una acá, el `update` falla.
- **Una regla que borre los backups viejos.** Los caduca
  `manage.py backup_database` (`DB_BACKUP_RETENTION_DAYS`, 30 días), que sabe
  algo que una regla por antigüedad no puede saber: **nunca borrar el último
  volcado que queda.** Si el job diario estuvo caído dos meses, lo que hay es
  viejo, pero es todo lo que hay. Una regla de ciclo de vida lo borraría por la
  fecha justo cuando más falta hace.
