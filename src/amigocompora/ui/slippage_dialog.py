"""El diálogo del deslizamiento por defecto: la tolerancia con la que se construye.

Es lo que abre el engranaje de la tarjeta de intercambio. Pide un porcentaje y lo
devuelve en puntos básicos, que es la unidad con la que el resto de la aplicación
habla de tolerancias (`BasisPoints`, `ExecutionLimits.slippage_bps`).

### Por qué se explica lo que hace

Este número decide el mínimo que un swap acepta recibir y mucha gente lo sube
«para que no revierta»: es cierto que subirlo reduce los reveses, y también que
cada décima de más es dinero que se acepta perder si el precio se mueve. El
diálogo lo dice con esas dos caras, sin recomendaciones: la decisión es del
usuario, y para tomarla tiene que saber qué está eligiendo.

### Qué no hace

No aplica nada por su cuenta. El valor viaja a quien abrió el diálogo, que es
quien lo fija en la copia viva y lo guarda en `config.toml`: si la escritura del
archivo falla, eso se cuenta fuera, no aquí.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from amigocompora.ui.theme import COLOR_MUTED

#: El mínimo que el campo admite, en por ciento. Cero no se ofrece: un swap con
#: tolerancia cero exige el importe exacto de la cotización y revierte ante
#: cualquier movimiento mínimo, que es un ajuste que sólo parece prudente.
MIN_PERCENT = 0.01

#: El máximo, en por ciento. Por encima del 50 % la tolerancia ya no protege de
#: nada: se estaría aceptando recibir la mitad de lo cotizado.
MAX_PERCENT = 50.0


class SlippageDialog(QDialog):
    """Pide la tolerancia de deslizamiento por defecto, en porcentaje."""

    def __init__(self, bps: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Deslizamiento por defecto — Amigocompora")
        self.setModal(True)
        self.setMinimumWidth(430)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        expl = QLabel(
            "Es la tolerancia con la que se construye el swap: <b>el mínimo que "
            "aceptas recibir</b>. Cuanto más baja, más te protege de un "
            "movimiento del precio y más fácil es que la operación revierta; "
            "cuanto más alta, menos reveses y más importe aceptas perder si el "
            "precio se mueve."
        )
        expl.setTextFormat(Qt.RichText)
        expl.setWordWrap(True)
        lay.addWidget(expl)

        fila = QHBoxLayout()
        fila.addWidget(QLabel("Deslizamiento tolerado:"))
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setRange(MIN_PERCENT, MAX_PERCENT)
        self._spin.setSingleStep(0.05)
        self._spin.setSuffix(" %")
        # El valor vigente, en %: el campo habla por ciento y el resto de la
        # aplicación por puntos básicos; la conversión vive en `value_bps`. Un
        # valor por debajo del mínimo del campo —un 0 % guardado a mano— se
        # enseña como el mínimo, porque es lo más parecido que el campo puede
        # escribir sin mentir.
        self._spin.setValue(max(bps, round(MIN_PERCENT * 100)) / 100)
        self._spin.setToolTip(
            "Porcentaje sobre lo cotizado que se acepta perder como máximo. "
            "0,5 % es la tolerancia que traía la aplicación."
        )
        fila.addWidget(self._spin)
        fila.addStretch()
        lay.addLayout(fila)

        almacen = QLabel(
            "Se guarda en config.toml y se aplica al construir los swaps de esta "
            "y de las próximas sesiones."
        )
        almacen.setStyleSheet(f"color: {COLOR_MUTED};")
        almacen.setWordWrap(True)
        lay.addWidget(almacen)

        caja = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        caja.button(QDialogButtonBox.Ok).setText("Guardar")
        caja.button(QDialogButtonBox.Cancel).setText("Cancelar")
        caja.accepted.connect(self.accept)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def value_bps(self) -> int:
        """La tolerancia elegida, en puntos básicos (0,50 % → 50)."""
        return round(self._spin.value() * 100)

    @property
    def spin(self) -> QDoubleSpinBox:
        """El campo, para las pruebas."""
        return self._spin
