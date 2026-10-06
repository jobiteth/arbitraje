"""Arranque de la aplicación de escritorio (PySide6 + qasync).

Este es el único sitio donde Qt y `asyncio` se encuentran. `qasync.QEventLoop`
integra el event loop de Qt con el de asyncio: los `await` de los casos de uso
ceden el control a la UI sin bloquear y sin `QThread` ni señales manuales.

Flujo:

1. Se crea el `QApplication` y el `QEventLoop` de qasync.
2. Se construye el `Container` (app, motores, scheduler) de forma asíncrona.
3. Se construye `MainWindow` y se le inyecta el prompt de confirmación Qt.
4. Se arranca el scheduler con la tarea de barrido de pares vigilados.
5. `run_forever()` corre hasta que el usuario cierra la ventana.
"""

from __future__ import annotations

import asyncio
import sys

import qasync
import structlog
from PySide6.QtWidgets import QApplication

from amigocompora import __version__
from amigocompora.app.container import Container, build_container
from amigocompora.app.scheduler import DEFAULT_SCAN_INTERVAL, ScheduledTask
from amigocompora.domain.modes import Capability
from amigocompora.ui.main_window import MainWindow

_log = structlog.get_logger(__name__)


def _install_scheduled_tasks(container: Container) -> None:
    """Registra las tareas periódicas del scheduler.

    El barrido de pares vigilados se registra sólo si hay pares configurados:
    una tarea sin pares no hace nada y sólo añadiría ruido al log.
    """
    if not container.watch_scan.pairs:
        return

    async def _scan() -> None:
        await container.watch_scan.run_once()

    container.scheduler.register(
        ScheduledTask(
            task_id="watch_scan",
            name="Barrido de pares vigilados",
            capability=Capability.COMPUTE_ROUTE,
            factory=_scan,
            interval_seconds=DEFAULT_SCAN_INTERVAL,
            jitter_seconds=3.0,
        )
    )


async def _build_and_show() -> tuple[Container, MainWindow]:
    container = await build_container()
    _install_scheduled_tasks(container)
    window = MainWindow(container, container.alert_center)
    window.show()
    container.scheduler.start()
    return container, window


def main() -> int:
    """Entrada del script `amigocompora`."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Amigocompora")
    app.setApplicationVersion(__version__)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    shutdown = asyncio.Event()
    app.aboutToQuit.connect(shutdown.set)

    async def _serve() -> None:
        container, _window = await _build_and_show()
        _log.info("app.started", version=__version__)
        await shutdown.wait()
        await container.aclose()
        loop.stop()

    with loop:
        loop.run_until_complete(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
