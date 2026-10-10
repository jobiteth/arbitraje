"""El «sí» del usuario llega por señal, no por un bucle de eventos anidado.

La regresión del cuelgue del 2026-10-10: `QtConfirmationPrompt.ask` llamaba a
`QDialog.exec()` dentro de una corrutina. Ese `exec()` abre un bucle de eventos
anidado **dentro** del paso de la corrutina y, bajo qasync, mata el despertar de
cualquier otra tarea en vuelo («Cannot enter into task … while another task … is
being executed»): la pestaña de puentes se quedó colgada a mitad de una
ejecución. Estas pruebas fijan el contrato bueno —`open()` + señal `finished`—
prohibiendo `exec()` de forma que reintroducirlo se note en el acto.

Qt necesita un `QApplication` vivo; en una máquina sin pantalla eso se resuelve
con la plataforma `offscreen`, fijada aquí antes de importar nada de Qt.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from amigocompora.app.confirmation import PendingAction
from amigocompora.domain.modes import Capability
from amigocompora.ui.widgets import ConfirmationDialog, QtConfirmationPrompt


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _accion() -> PendingAction:
    return PendingAction(
        action_id="prueba",
        capability=Capability.BROADCAST_TX,
        title="Emitir la transacción de prueba",
        requested_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )


def _prohibir_exec(monkeypatch: pytest.MonkeyPatch, *, aceptar: bool) -> None:
    """Cambia `exec` por un fallo y `open` por una apertura que se contesta sola.

    La respuesta la resuelve la señal `finished` real —la de aceptar o
    cancelar—; aquí se dispara en cuanto el diálogo queda abierto, que es el
    momento en el que el usuario lo haría. Antes del arreglo, `ask` caía en el
    `exec` prohibido y la prueba se ponía roja con el porqué escrito.
    """

    def prohibido(self: ConfirmationDialog) -> int:
        raise AssertionError(
            "ask() llamó a QDialog.exec(): un bucle anidado dentro de la corrutina "
            "fue la causa del cuelgue del 2026-10-10"
        )

    def abrir(self: ConfirmationDialog) -> None:
        cerrar = self.accept if aceptar else self.reject
        asyncio.get_running_loop().call_soon(cerrar)

    monkeypatch.setattr(ConfirmationDialog, "exec", prohibido)
    monkeypatch.setattr(ConfirmationDialog, "open", abrir)


async def test_el_si_llega_por_la_senal_sin_bloquear_el_bucle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prohibir_exec(monkeypatch, aceptar=True)
    prompt = QtConfirmationPrompt(QWidget())

    # Otra tarea cualquiera tiene que poder correr mientras el diálogo está
    # abierto: es exactamente lo que el bucle anidado impedía.
    corrio = asyncio.Event()

    async def _otra() -> None:
        corrio.set()

    otra = asyncio.create_task(_otra())

    assert await asyncio.wait_for(prompt.ask(_accion()), timeout=1) is True
    await asyncio.wait_for(otra, timeout=1)
    assert corrio.is_set()


async def test_el_no_llega_por_la_senal(monkeypatch: pytest.MonkeyPatch) -> None:
    _prohibir_exec(monkeypatch, aceptar=False)
    prompt = QtConfirmationPrompt(QWidget())

    assert await asyncio.wait_for(prompt.ask(_accion()), timeout=1) is False
