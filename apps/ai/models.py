"""
Registro de consumo de IA.

Una fila por llamada a Gemini. Sirve para dos cosas a la vez, y por eso es
una sola tabla y no un log más un contador:

1. **Es el contador de la cuota.** "Cuántos recibos leyó este usuario este
   mes" es un COUNT sobre esta tabla. Un contador aparte tendría que
   mantenerse sincronizado con el log y en algún momento se iría de mano;
   acá no hay dos fuentes que puedan discrepar.
2. **Es el detalle de lo que se gasta**, por operación y por modelo, para
   poder mirar la factura de Google y saber de dónde salió.

**No guarda nada de lo que el usuario escribió ni de lo que la IA
respondió** — ni el texto, ni la imagen, ni el resultado. Sólo metadatos:
qué operación, qué modelo, cuántos tokens, cuánto tardó y si salió bien.
Un backup de esta tabla no filtra las finanzas de nadie.
"""
from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel

# Operaciones. El valor se guarda en base y se usa como clave de cuota, así
# que agregar una es agregar también su entrada en `quotas.PLAN_FEATURE_KEYS`.
OP_RECEIPT = "receipt"
OP_PARSE = "parse"
OP_CHAT = "chat"
OP_SUMMARY = "summary"
OP_CHOICES = [
    (OP_RECEIPT, "Lectura de recibo"),
    (OP_PARSE, "Parseo de texto o voz"),
    (OP_CHAT, "Pregunta de chat"),
    (OP_SUMMARY, "Resumen mensual"),
]

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CHOICES = [
    (STATUS_OK, "Respondió"),
    (STATUS_ERROR, "Falló"),
]


class AIUsage(TimeStampedModel):
    """Una llamada a la API de Gemini, salga bien o mal."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_usage"
    )
    # Opcional porque hay operaciones que no nacen de un workspace concreto
    # (el resumen mensual se dispara por usuario). Sirve para saber en qué
    # presupuesto se está gastando, no para permisos.
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_usage",
    )

    operation = models.CharField(max_length=16, choices=OP_CHOICES)
    model = models.CharField(max_length=60, help_text="Modelo de Gemini que atendió la llamada.")
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=STATUS_OK)
    # Código corto del error (`timeout`, `http_429`, `invalid_json`...), nunca
    # el cuerpo de la respuesta: puede traer el prompt de vuelta.
    error_code = models.CharField(max_length=40, blank=True)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    # En millonésimas de dólar: una lectura de recibo son ~1.700 y guardarlo
    # en decimal de 2 posiciones lo redondearía a cero. Sumar la columna da
    # el gasto del mes sin perder nada por el camino.
    cost_micros = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)

    # Si esta llamada consume cuota. Las que ni llegaron a Gemini (cuota
    # agotada, validación) no se registran; las que llegaron y fallaron del
    # lado de ellos sí se registran pero no se cobran al usuario, que no
    # tiene la culpa.
    counts_against_quota = models.BooleanField(default=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            # El índice que hace barato el COUNT de la cuota, que es la
            # consulta que corre en cada request de IA.
            models.Index(fields=["user", "operation", "created_at"]),
        ]
        verbose_name = "consumo de IA"
        verbose_name_plural = "consumos de IA"

    def __str__(self):
        return f"{self.user} · {self.get_operation_display()} · {self.model}"

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / 1_000_000
