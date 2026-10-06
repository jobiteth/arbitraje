"""Logging estructurado con redacción de secretos.

La redacción no es decorativa. Un log de esta aplicación contiene URLs de RPC
—que en los proveedores habituales llevan la API key **dentro de la ruta**,
como `https://mainnet.infura.io/v3/<KEY>`— y respuestas de APIs con tokens en
las cabeceras. Un fichero de log compartido para diagnosticar un fallo no debe
filtrar credenciales.

Dos procesadores:

- `redact_sensitive_keys`: enmascara cualquier campo cuyo nombre sugiera un
  secreto, a cualquier profundidad.
- `redact_urls`: reduce las URLs a esquema + host, descartando ruta y query,
  que es donde viajan las claves.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final
from urllib.parse import urlsplit

import structlog

#: Máscara que se escribe en lugar del valor. Longitud fija: la longitud real
#: de un secreto también es información.
MASK: Final = "***"

#: Fragmentos que, si aparecen en el nombre de un campo, lo marcan como secreto.
SENSITIVE_HINTS: Final = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "mnemonic",
        "passphrase",
        "password",
        "private_key",
        "privatekey",
        "secret",
        "seed",
        "token",
    }
)

#: Campos cuyo valor se trata como URL y se recorta a esquema + host.
URL_FIELDS: Final = frozenset({"endpoint", "endpoint_url", "host", "url"})


def is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in SENSITIVE_HINTS)


def safe_url(url: str) -> str:
    """`https://host` a partir de una URL completa.

    Descarta ruta, query y credenciales embebidas: en los RPC comerciales la
    API key va justo ahí.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme or not parts.hostname:
        return MASK
    port = f":{parts.port}" if parts.port else ""
    suffix = "/…" if parts.path not in {"", "/"} or parts.query else ""
    return f"{parts.scheme}://{parts.hostname}{port}{suffix}"


def _redact_value(key: str, value: Any) -> Any:
    if is_sensitive_name(key):
        return MASK
    if isinstance(value, MutableMapping):
        return {inner: _redact_value(inner, item) for inner, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(key, item) for item in value)
    if key.lower() in URL_FIELDS and isinstance(value, str):
        return safe_url(value)
    return value


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Procesador structlog: enmascara secretos y recorta URLs."""
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def configure_logging(*, level: str = "INFO", as_json: bool = False) -> None:
    """Configura structlog y el logging estándar. Idempotente.

    `as_json=True` para ficheros y CI; consola legible por defecto.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric_level,
        force=True,
    )

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if as_json
        else structlog.dev.ConsoleRenderer(colors=not as_json)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # La redacción va antes del renderizado y después de añadir
            # contexto, para que también cubra lo que inyecten los binds.
            redact_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
