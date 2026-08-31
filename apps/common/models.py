import uuid
from django.db import models


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
