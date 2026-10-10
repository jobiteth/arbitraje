"""Candado de una ejecución a la vez.

Firmar y emitir es la única operación de esta aplicación que no se puede
deshacer, y dos ejecuciones a la vez sobre la misma cartera se estorban de la
peor manera: se construyen con el mismo nonce —sólo una puede minar— y suman
dos operaciones donde el usuario autorizó una. Medido en vivo (2026-10-10): un
segundo disparo durante un puente en curso produjo dos firmas con el mismo
nonce antes de que nada llegara a emitirse.

`SingleFlight` es la puerta: la primera ejecución entra; la segunda **se
rechaza con un error**, no se encola. Encolar sería lo cómodo y es lo
peligroso: una operación que se firma sola «cuando le toque el turno» es una
firma que el usuario ya no está mirando. Se rechaza y el usuario decide de
nuevo, con el estado delante.

Vive en `app` y no en la UI a propósito: una pestaña puede olvidarse de su
bandera —ya pasó—, pero la puerta que de verdad protege el dinero no puede
depender de que una vista se acuerde de repintar un botón.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from amigocompora.domain.errors import ExecutionError


class SingleFlight:
    """Deja pasar una ejecución a la vez; la segunda se rechaza."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._what = ""

    @property
    def busy(self) -> bool:
        """Si hay una ejecución en curso."""
        return self._lock.locked()

    @asynccontextmanager
    async def exclusive(self, *, what: str) -> AsyncIterator[None]:
        """Toma el turno, o lanza `ExecutionError` si ya hay uno en curso.

        `what` describe lo que se está lanzando («un puente», «un swap»); el
        rechazo nombra lo que está **en curso**, que es lo que el usuario
        necesita saber para decidir qué hacer mientras espera.

        El chequeo y la toma van seguidos y sin `await` en medio, así que en el
        único hilo de asyncio no hay ventana de carrera: o se entra, o se
        rechaza.
        """
        if self._lock.locked():
            en_curso = self._what or "una operación"
            raise ExecutionError(
                f"Ya hay {en_curso} en curso y sólo se firma una operación a la "
                "vez. La segunda no se encola —nadie debe firmar sin que lo estés "
                "mirando—: espera a que termine y repítela tú."
            )
        async with self._lock:
            self._what = what
            try:
                yield
            finally:
                self._what = ""
