"""El desplegable de modo de la barra superior.

Existe por el fallo más caro que ha tenido esta interfaz, y por una razón que no
es de estilo: **ninguna prueba tocaba el desplegable**. Las 1160 pruebas pasaban
mientras la aplicación se rompía en cuanto alguien elegía un modo. Así que lo que
se comprueba aquí es el camino real —el `QComboBox` de la ventana de verdad— y no
`set_mode` a mano, que es como lo hacían las que ya había y por eso no lo vieron.

El fallo, medido: `QComboBox.addItem(etiqueta, modo)` guarda el `OperationMode`
como texto —es un `StrEnum`, o sea un `str`— y `currentData()` lo devuelve como
texto. El guard se quedaba con la cadena dentro y la siguiente consulta de
permisos reventaba con `'str' object has no attribute 'grants'`: dejaban de
funcionar a la vez la cotización, los puentes y los mercados. La aplicación
entera, por elegir un modo en un desplegable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from amigocompora.app.container import Container, build_container
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import InMemorySecretStore
from amigocompora.ui.main_window import MainWindow


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _ventana() -> AsyncIterator[tuple[Container, MainWindow]]:
    container = await build_container(
        Settings(), secret_store=InMemorySecretStore(), configure_logs=False
    )
    try:
        yield container, MainWindow(container, container.alert_center)
    finally:
        await container.aclose()


def _elegir(window: MainWindow, mode: OperationMode) -> None:
    """Elige un modo **como lo haría una persona**: moviendo el desplegable."""
    window._mode_combo.setCurrentIndex(window._mode_index(mode))


async def test_elegir_un_modo_deja_el_guard_con_el_modo_de_verdad() -> None:
    """En el guard tiene que quedar el modo, no el texto que suelta Qt."""
    async with _ventana() as (container, window):
        _elegir(window, OperationMode.SIMULATION)
        assert container.guard.mode is OperationMode.SIMULATION


async def test_tras_cambiar_de_modo_los_permisos_se_siguen_pudiendo_consultar() -> None:
    """El fallo literal: cambiar de modo y, acto seguido, preguntar por permisos.

    Es lo que hacían las tres pantallas un instante después de que alguien
    eligiera un modo, porque todas consultan `BROADCAST_TX` al pintar su tarjeta
    de ejecución. Antes de esto, la consulta lanzaba `AttributeError`.
    """
    async with _ventana() as (container, window):
        _elegir(window, OperationMode.SIMULATION)
        assert container.guard.allows(Capability.READ_CHAIN) is True
        assert container.guard.allows(Capability.BROADCAST_TX) is False


async def test_subir_a_ejecucion_desde_el_desplegable_concede_firmar_y_emitir() -> None:
    """El camino que importa: el modo que mueve dinero se elige desde aquí."""
    async with _ventana() as (container, window):
        _elegir(window, OperationMode.EXECUTION)
        assert container.guard.mode is OperationMode.EXECUTION
        assert container.guard.allows(Capability.SIGN_TX)
        assert container.guard.allows(Capability.BROADCAST_TX)


async def test_el_desplegable_sigue_al_guard_y_la_vuelta_no_lo_estropea() -> None:
    """El botón «Cambiar a Ejecución →» mueve el guard, y el desplegable lo sigue.

    `setCurrentIndex` vuelve a emitir el cambio de índice, así que éste es
    también el camino por el que el desplegable podría devolverle la cadena al
    guard. Después de la vuelta completa, el guard tiene que seguir entero y
    concediendo lo mismo.
    """
    async with _ventana() as (container, window):
        container.guard.set_mode(OperationMode.EXECUTION)
        assert window._mode_combo.currentIndex() == window._mode_index(OperationMode.EXECUTION)
        assert container.guard.mode is OperationMode.EXECUTION
        assert container.guard.allows(Capability.BROADCAST_TX)
