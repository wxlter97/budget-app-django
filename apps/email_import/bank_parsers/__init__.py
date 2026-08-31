"""
Parsers de notificaciones bancarias.

Agregar soporte para un banco nuevo:
  1. crear ``bank_parsers/<banco>.py`` con una función decorada con
     ``@register("<slug-del-banco>")`` que devuelva un ``ParsedEmail``
     o lance ``ParseError``;
  2. importarla abajo para que se registre;
  3. crear un ``BankEmailSchema`` cuyo ``bank_name`` slugificado sea ese slug.
"""
from .base import ParsedEmail, ParseError  # noqa: F401
from .registry import get_parser, registered_keys  # noqa: F401

# Importa cada módulo de parser para que se registre en el import de la app.
from . import demo_bank  # noqa: F401,E402
from . import nu_style  # noqa: F401,E402
