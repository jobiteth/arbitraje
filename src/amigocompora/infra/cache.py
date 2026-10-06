"""Caché con TTL y expulsión LRU.

Pensada para cotizaciones: un TTL corto (segundos) evita machacar los endpoints
cuando el usuario cambia de pestaña, pero no tanto como para mostrar un precio
viejo. Un precio caducado es peor que no tener precio, así que el TTL expulsa
por tiempo y no sólo por tamaño.

El reloj se inyecta para que los tests de expiración no duerman.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from amigocompora.domain.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    expired: int
    size: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return 0.0 if total == 0 else self.hits / total


class TtlCache[K, V]:
    """Caché acotada con expiración por tiempo y expulsión del menos usado."""

    __slots__ = ("_clock", "_entries", "_expired", "_hits", "_maxsize", "_misses", "_ttl")

    def __init__(
        self,
        ttl_seconds: float,
        *,
        maxsize: int = 256,
        clock: Clock | None = None,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"el TTL no puede ser negativo, llegó {ttl_seconds}")
        if maxsize < 1:
            raise ValueError(f"maxsize debe ser >= 1, llegó {maxsize}")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._maxsize = maxsize
        self._clock = clock or SystemClock()
        self._entries: OrderedDict[K, tuple[datetime, V]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._expired = 0

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        expires_at, value = entry
        if self._clock.now() >= expires_at:
            del self._entries[key]
            self._expired += 1
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: K, value: V) -> None:
        """Guarda un valor. Con `ttl=0` la caché queda deshabilitada de hecho."""
        if not self._ttl:
            return
        self._entries[key] = (self._clock.now() + self._ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def invalidate(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            expired=self._expired,
            size=len(self._entries),
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: K) -> bool:
        return self.get(key) is not None
