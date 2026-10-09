"""Diario de avisos y errores de la sesión, para leerlos en la pestaña Errores.

Se alimenta del propio flujo de structlog, **después** de la redacción de
secretos: lo que queda aquí es lo mismo que sale por el log, y nunca una clave.
Vive en memoria y tiene capacidad fija, así que no crece sin límite ni deja nada
en disco.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from structlog.typing import EventDict, WrappedLogger

CAPACIDAD: Final = 500

_NIVELES_REGISTRADOS: Final = frozenset({"warning", "warn", "error", "critical", "exception"})
_CAMPOS_DE_SISTEMA: Final = frozenset({"event", "level", "timestamp"})


@dataclass(frozen=True, slots=True)
class EntradaDiario:
    momento: datetime
    nivel: str
    evento: str
    detalles: tuple[tuple[str, str], ...]


class DiarioErrores:
    def __init__(self, capacidad: int = CAPACIDAD) -> None:
        self._entradas: deque[EntradaDiario] = deque(maxlen=capacidad)

    def registrar(self, nivel: str, evento: str, detalles: Iterable[tuple[str, str]]) -> None:
        self._entradas.append(
            EntradaDiario(
                momento=datetime.now(UTC),
                nivel=nivel,
                evento=evento,
                detalles=tuple(detalles),
            )
        )

    def entradas(self) -> tuple[EntradaDiario, ...]:
        """Las entradas de la más antigua a la más reciente."""
        return tuple(self._entradas)

    def vaciar(self) -> None:
        self._entradas.clear()


DIARIO: Final = DiarioErrores()


def capturar_errores(logger: WrappedLogger, metodo: str, evento: EventDict) -> EventDict:
    """Procesador de structlog: guarda los avisos y errores y deja pasar el evento."""
    nivel = str(evento.get("level", metodo))
    if nivel in _NIVELES_REGISTRADOS:
        detalles = [
            (clave, _texto(valor))
            for clave, valor in evento.items()
            if clave not in _CAMPOS_DE_SISTEMA
        ]
        DIARIO.registrar(nivel, str(evento.get("event", "")), detalles)
    return evento


def _texto(valor: Any) -> str:
    return valor if isinstance(valor, str) else repr(valor)
