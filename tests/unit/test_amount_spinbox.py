"""El campo de importe: se teclea con punto —o con coma— y se lee sin ceros.

Regresión medida el 2026-10-10. En el campo del puente, escribir «0.000243» lo
convertía en «24»: el campo **mostraba** con punto pero dejaba que Qt
**interpretara** con la configuración regional del sistema —en español el punto
es el separador de millares—, así que los dígitos se concatenaban («0000243» →
243, y a mitad del tecleo, 24). En el resto de los campos numéricos la trampa
era la misma al revés: con la coma como separador decimal del sistema, teclear
«0.5» daba «5» — diez veces el importe, sin decirlo.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla —CI, un
servidor— eso se resuelve con la plataforma `offscreen`, que se fija aquí antes
de que se importe nada de Qt.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amigocompora.ui.widgets import AmountSpinBox


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _campo(*, decimals: int = 6, minimo: float = 0.000001) -> AmountSpinBox:
    box = AmountSpinBox()
    box.setDecimals(decimals)
    box.setRange(minimo, 1_000_000.0)
    box.clear()
    return box


def _teclear(box: AmountSpinBox, texto: str) -> None:
    """Teclea en el editor del campo, que es donde caen las teclas de verdad."""
    editor = box.lineEdit()
    assert editor is not None
    QTest.keyClicks(editor, texto)


def test_el_punto_es_el_separador_decimal_al_teclear() -> None:
    """El caso reportado: «0.000243» se quedaba en «24» a mitad del tecleo."""
    box = _campo()
    _teclear(box, "0.000243")
    assert box.value() == pytest.approx(0.000243)
    assert box.text() == "0.000243"


def test_la_coma_tambien_vale_como_separador_decimal() -> None:
    """En un teclado español la tecla decimal del bloque numérico escribe coma."""
    box = _campo()
    _teclear(box, "0,000243")
    assert box.value() == pytest.approx(0.000243)
    box.interpretText()  # lo que hace Enter o confirmar el campo
    assert box.text() == "0.000243", "al confirmar se muestra con punto, como el resto de la app"


def test_el_importe_pequeno_no_se_multiplica_por_diez() -> None:
    """La otra dirección de la trampa: teclear «0.5» daba «5»."""
    box = _campo(decimals=2, minimo=0.01)
    _teclear(box, "0.5")
    assert box.value() == pytest.approx(0.5)
    assert box.text() == "0.5"


def test_la_pantalla_recorta_los_ceros_de_relleno() -> None:
    box = _campo()
    box.setValue(5.0)
    assert box.text() == "5"
    box.setValue(0.5)
    assert box.text() == "0.5"
