"""Scheduler asíncrono de tareas periódicas.

Etapa 4: Motor de tareas programadas y modos de operación.

No usa APScheduler: el grafo es pequeño y el control fino importa. Cada tarea
declara la `Capability` que necesita; el scheduler consulta al `ModeGuard` antes
de ejecutarla. Si el modo no la concede, la ejecución se omite y se registra —
no se intenta «subir de modo».

Propiedades clave:

- **No solapa ejecuciones.** Si una tarea sigue corriendo cuando toca el siguiente
  tick, se omite ese tick. Un barrido que tarda 8 s con intervalo de 5 s no
  duplica peticiones a la API, que es justo lo que dispararía un 429.
- **Jitter.** Evita que varias tareas disparadas al arrancar se alineen para
  siempre y golpeen la red en ráfaga.
- **Reloj inyectable.** Para tests que no quieren dormir.
- **Ciclo de vida explícito.** `start()` crea las corrutinas, `stop()` las
  cancela y espera su terminación. Sin hilos ni procesos: todo es `asyncio`.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

import structlog

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.modes import Capability

_log = structlog.get_logger(__name__)


class TaskStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(slots=True)
class TaskStats:
    runs: int = 0
    skipped_mode: int = 0
    skipped_overlap: int = 0
    failures: int = 0
    last_run_at: datetime | None = None
    last_error: str = ""
    last_duration_seconds: float = 0.0


@dataclass(slots=True)
class ScheduledTask:
    """Definición de una tarea periódica.

    `factory` devuelve el awaitable a ejecutar en cada tick. Es fábrica y no
    corrutina directa para que cada tick tenga su propio awaitable y no se
    reutilice uno ya consumido.
    """

    task_id: str
    name: str
    capability: Capability
    factory: Callable[[], Awaitable[None]]
    interval_seconds: float
    jitter_seconds: float = 0.0
    enabled: bool = True
    stats: TaskStats = field(default_factory=TaskStats)
    status: TaskStatus = TaskStatus.IDLE

    def next_delay(self) -> float:
        if self.jitter_seconds <= 0:
            return self.interval_seconds
        return self.interval_seconds + random.uniform(  # noqa: S311
            -self.jitter_seconds, self.jitter_seconds
        )


class Scheduler:
    """Conjunto de tareas periódicas con respeto a `ModeGuard`."""

    def __init__(
        self,
        guard: ModeGuard,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._guard = guard
        self._clock = clock or SystemClock()
        self._tasks: dict[str, ScheduledTask] = {}
        self._handles: dict[str, asyncio.Task[None]] = {}
        self._running = False

    # -- registro -----------------------------------------------------------

    def register(self, task: ScheduledTask) -> ScheduledTask:
        if task.task_id in self._tasks:
            raise ValueError(f"tarea «{task.task_id}» ya registrada")
        if task.interval_seconds <= 0:
            raise ValueError(f"intervalo de «{task.task_id}» debe ser > 0")
        self._tasks[task.task_id] = task
        _log.info("scheduler.registered", task_id=task.task_id, interval=task.interval_seconds)
        if self._running and task.enabled:
            self._handles[task.task_id] = asyncio.create_task(
                self._loop(task), name=f"scheduler:{task.task_id}"
            )
        return task

    def unregister(self, task_id: str) -> None:
        self.pause(task_id)
        self._tasks.pop(task_id, None)

    def task(self, task_id: str) -> ScheduledTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"tarea «{task_id}» no registrada") from None

    def tasks(self) -> tuple[ScheduledTask, ...]:
        return tuple(self._tasks.values())

    # -- control ------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for task in self._tasks.values():
            if task.enabled and task.task_id not in self._handles:
                self._handles[task.task_id] = asyncio.create_task(
                    self._loop(task), name=f"scheduler:{task.task_id}"
                )
        _log.info("scheduler.started", count=len(self._handles))

    async def stop(self) -> None:
        self._running = False
        handles = list(self._handles.values())
        self._handles.clear()
        for handle in handles:
            handle.cancel()
        for handle in handles:
            with contextlib.suppress(asyncio.CancelledError):
                await handle
        _log.info("scheduler.stopped")

    def pause(self, task_id: str) -> None:
        task = self.task(task_id)
        task.status = TaskStatus.PAUSED
        handle = self._handles.pop(task_id, None)
        if handle is not None:
            handle.cancel()

    def resume(self, task_id: str) -> None:
        task = self.task(task_id)
        if task.status != TaskStatus.PAUSED:
            return
        task.status = TaskStatus.IDLE
        if self._running and task.enabled:
            self._handles[task_id] = asyncio.create_task(
                self._loop(task), name=f"scheduler:{task.task_id}"
            )

    async def trigger_now(self, task_id: str) -> None:
        """Ejecuta una tarea fuera de su intervalo, respetando modo y solape."""
        task = self.task(task_id)
        await self._tick(task)

    # -- loop interno -------------------------------------------------------

    async def _loop(self, task: ScheduledTask) -> None:
        # Espera inicial con jitter para desfasar tareas que arrancan juntas.
        if task.jitter_seconds > 0:
            await asyncio.sleep(random.uniform(0, task.jitter_seconds))  # noqa: S311
        while self._running and task.task_id in self._tasks:
            delay = task.next_delay()
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if task.status == TaskStatus.PAUSED or not task.enabled:
                continue
            await self._tick(task)

    async def _tick(self, task: ScheduledTask) -> None:
        if task.status == TaskStatus.RUNNING:
            task.stats.skipped_overlap += 1
            _log.debug("scheduler.skipped_overlap", task_id=task.task_id)
            return
        if not self._guard.allows(task.capability):
            task.stats.skipped_mode += 1
            _log.info(
                "scheduler.skipped_mode",
                task_id=task.task_id,
                mode=self._guard.mode.value,
                capability=task.capability.value,
            )
            return
        task.status = TaskStatus.RUNNING
        started = self._clock.now()
        try:
            await task.factory()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            task.stats.failures += 1
            task.status = TaskStatus.FAILED
            task.stats.last_error = str(error)[:400]
            _log.warning("scheduler.task_failed", task_id=task.task_id, reason=str(error))
            # Vuelve a IDLE para que el siguiente tick reintente.
            task.status = TaskStatus.IDLE
            return
        finally:
            if task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.IDLE
        elapsed = (self._clock.now() - started).total_seconds()
        task.stats.runs += 1
        task.stats.last_run_at = self._clock.now()
        task.stats.last_duration_seconds = elapsed
        task.stats.last_error = ""
        _log.debug("scheduler.tick_ok", task_id=task.task_id, elapsed=round(elapsed, 3))

    @property
    def is_running(self) -> bool:
        return self._running


#: Intervalos por defecto (segundos). Cortos para que la UI parezca viva sin
#: machacar las APIs: el TTL de caché absorbe la mayoría de los ticks.
DEFAULT_SCAN_INTERVAL: Final = 30.0
DEFAULT_MARKET_INTERVAL: Final = 60.0
DEFAULT_RPC_HEALTH_INTERVAL: Final = 30.0
