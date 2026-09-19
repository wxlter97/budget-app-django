import uuid
from django.db import models


class ModuleFlag(models.Model):
    """
    Interruptor manual por módulo: si algo empieza a fallar en producción
    (una integración de terceros, un parser frágil...), togglear ``enabled``
    acá lo apaga al instante para todo el mundo -- sin deploy, sin esperar a
    la próxima build de la app.

    Deliberadamente NO usa `BaseModel`/soft-delete: es un catálogo chico que
    se edita a mano desde el admin (`list_editable` en la lista, ver
    `apps.common.admin`), no un recurso de dominio con historial.

    Fail-open igual que `apps.billing.services` (ver docstring ahí): un
    ``key`` sin fila todavía en esta tabla se considera habilitado -- así
    agregar el chequeo en un endpoint nuevo no exige primero crear la fila.
    """

    key = models.SlugField(
        max_length=60, unique=True,
        help_text="Clave estable que usa el código para preguntar (p. ej. \"ai\", "
                   "\"email_import\", \"excel_import\"). No se traduce ni se muestra.",
    )
    label = models.CharField(max_length=100, help_text="Nombre para reconocerlo en esta lista.")
    is_enabled = models.BooleanField(default=True)
    disabled_message = models.TextField(
        blank=True,
        help_text="Lo que ve la persona mientras está apagado. Vacío = mensaje genérico.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        verbose_name = "interruptor de módulo"
        verbose_name_plural = "interruptores de módulos"

    def __str__(self):
        return f"{self.label} ({self.key})"


class TimeStampedModel(models.Model):
    """Base con auditoria: created_at / updated_at en todos los modelos."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)


class SoftDeleteManager(models.Manager):
    """Por defecto solo devuelve registros no borrados.
    Usa Model.all_objects para incluir los borrados (auditoria/recuperacion)."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()


class BaseModel(TimeStampedModel):
    """
    Base para (casi) todos los modelos del dominio:
    - UUID como PK (facilita exportar/fusionar datos entre workspaces despues)
    - soft delete (is_deleted) en vez de borrado fisico
    - auditoria created_at/updated_at heredada de TimeStampedModel
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    is_deleted = models.BooleanField(default=False)

    objects = SoftDeleteManager()      # queryset filtrado (default)
    all_objects = models.Manager()      # queryset sin filtrar, para admin/auditoria

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.save(update_fields=["is_deleted", "updated_at"])


class WorkspaceScopedQuerySet(SoftDeleteQuerySet):
    def for_workspace(self, workspace):
        return self.filter(workspace=workspace, is_deleted=False)


class WorkspaceScopedManager(models.Manager):
    """
    Manager que fuerza el filtrado por workspace. No reemplaza la necesidad
    de pasar siempre el workspace explicito en cada vista/serializer, pero
    hace mucho mas dificil escribir un query que se le olvide filtrar.
    """

    def get_queryset(self):
        return WorkspaceScopedQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def for_workspace(self, workspace):
        return self.get_queryset().filter(workspace=workspace)


class WorkspaceScopedModel(BaseModel):
    """
    Base para cualquier modelo que cuelgue de un Workspace (el "presupuesto"
    compartido). Todas las entidades del dominio (cuentas, transacciones,
    categorias, etc.) heredan de aqui en vez de tener un FK a Workspace
    repetido y sin garantia de uso consistente.
    """
    class Meta:
        abstract = True
