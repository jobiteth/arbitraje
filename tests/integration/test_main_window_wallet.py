"""El aviso de la Cartera cuando cambia la cartera activa.

La Cartera dejó de ser una lista de saldos y es quien manda en «de quién es la
dirección que firma». Cambiar de cartera —o añadir, renombrar o borrar una—
tiene que llegar a las pantallas que enseñan ese dueño, swap y predicción, y
este es el cable que lo hace: sin él, los botones que firman seguirían pintados
para la cartera anterior, que es la peor forma de estar mal —diciendo que sí
con la que ya no es—.

Se comprueba el cable y no el efecto porque el efecto —el gate encendido o
apagado— ya se prueba en cada página: lo que aquí puede faltar es el aviso.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from amigocompora.app.container import Container, build_container
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import InMemorySecretStore
from amigocompora.ui.main_window import MainWindow
from amigocompora.ui.pages.prediction import PredictionPage
from amigocompora.ui.pages.prices import PricesPage


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


async def test_cambiar_de_cartera_avisa_a_las_pantallas_que_firman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El aviso tiene que llegar a swap y a predicción, en ese orden.

    Se sustituye el refresco **en la clase** y antes de construir la ventana:
    la conexión se hace al construir, así que un doble puesto después no
    escucharía nada —y la prueba pasaría midiendo un cable que no existe—.
    """
    avisos: list[str] = []
    monkeypatch.setattr(
        PricesPage, "refresh_execution_state", lambda self: avisos.append("swap")
    )
    monkeypatch.setattr(
        PredictionPage, "refresh_execution_state", lambda self: avisos.append("predicción")
    )

    async with _ventana() as (_container, window):
        # El pintado de arranque de la ventana no es el aviso que se mide.
        avisos.clear()
        window._wallet.wallet_changed.emit()

    assert avisos == ["swap", "predicción"]
