"""Reloj inyectable.

El dominio necesita marcas de tiempo (cuándo se observó un precio) pero no debe
leer el reloj del sistema directamente: hace los tests no deterministas. Los
casos de uso reciben un `Clock` y los tests inyectan `FrozenClock`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Fuente de la hora actual. Siempre con zona horaria."""

    def now(self) -> datetime: ...


class SystemClock:
    """Reloj real, en UTC. Las conversiones a hora local son cosa de la UI."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(slots=True)
class FrozenClock:
    """Reloj controlado, para tests. Avanza sólo cuando se le pide."""

    moment: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)
