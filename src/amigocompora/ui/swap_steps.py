"""La lista de las transacciones de una operación, con lo que pasó con cada una.

### Qué enseña

Una fila por paso real de la ejecución —cada aprobación, y el swap— más las dos
fases de narración (preparación y coste de red). Cada fila lleva su glifo de
estado, lo que hizo, su hash acortado —con el entero en el tooltip— y el enlace
al explorador de la red cuando existe.

Es la respuesta a «¿qué acaba de pasar?» después de una operación que puede
emitir tres transacciones seguidas: sin ella, el usuario ve el botón volver a su
sitio y tiene que reconstruir de memoria qué diálogos aceptó.

### Por qué no decide nada

El panel no consulta la red ni interpreta cifras: pinta lo que le llega en un
`StepUpdate` del caso de uso de ejecución, tal cual. Los rechazos y los fallos
también se listan, con su motivo, porque un «no» del usuario y un revert son
parte del resultado y esconderlos dejaría la lista contando media historia.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from amigocompora.app.usecases.execute_swap import StepState, StepUpdate, SwapStep
from amigocompora.domain.addresses import shorten
from amigocompora.ui.receipt_dialog import explorer_url
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_SUCCESS

#: El glifo de cada estado. El de «corriendo» es una elipsis porque eso es lo
#: único que se sabe: la transacción todavía no ha dicho cómo acabó.
_GLYPHS: dict[StepState, str] = {
    StepState.RUNNING: "…",
    StepState.DONE: "✓",
    StepState.REJECTED: "✕",
    StepState.FAILED: "✕",
}

#: El color del glifo de cada estado. El rechazo va en gris y no en rojo a
#: propósito: decidir no firmar no es una avería, y pintarlo como un error
#: empujaría a tratar una decisión legítima como algo que hay que arreglar.
_COLORS: dict[StepState, str] = {
    StepState.RUNNING: COLOR_MUTED,
    StepState.DONE: COLOR_SUCCESS,
    StepState.REJECTED: COLOR_MUTED,
    StepState.FAILED: COLOR_DANGER,
}


class _StepRow(QFrame):
    """Una fila del panel: glifo, título, detalle y enlace si el hash tiene uno."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("legBox")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self._glyph = QLabel("…")
        self._glyph.setFixedWidth(16)
        self._glyph.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 700;")
        lay.addWidget(self._glyph)

        self._title = QLabel("")
        lay.addWidget(self._title)

        # El detalle va detrás del título, en gris: el título dice qué pasó y el
        # detalle, a qué precio o por qué no pudo ser.
        self._detail = QLabel("")
        self._detail.setStyleSheet(f"color: {COLOR_MUTED};")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._detail, 1)

        self._link = QLabel("")
        self._link.setTextFormat(Qt.RichText)
        self._link.setOpenExternalLinks(True)
        lay.addWidget(self._link, 0, Qt.AlignVCenter)

    def apply(self, update: StepUpdate, chain_key: str) -> None:
        """Pinta el estado que trae el aviso. Se llama una vez por aviso."""
        glyph = _GLYPHS[update.state]
        color = _COLORS[update.state]
        self._glyph.setText(glyph)
        self._glyph.setStyleSheet(f"color: {color}; font-weight: 700;")
        self._title.setText(update.title)

        piezas: list[str] = []
        if update.detail:
            piezas.append(update.detail)
        if update.tx_hash:
            acortado = shorten(update.tx_hash)
            piezas.append(acortado)
            # El hash entero, en el tooltip: es lo que se copia a un explorador o
            # a un mensaje de soporte, y el recorte es sólo de la pantalla.
            self.setToolTip(update.tx_hash)
        self._detail.setText(" · ".join(piezas))

        enlace = explorer_url(chain_key, update.tx_hash) if update.tx_hash else None
        if enlace is not None:
            self._link.setText(f"<a href='{enlace}'>ver</a>")
        else:
            self._link.setText("")

    def title_text(self) -> str:
        """Lo que dice la fila ahora mismo."""
        return self._title.text()

    def glyph_text(self) -> str:
        """El glifo del estado vigente, para las pruebas."""
        return self._glyph.text()


class SwapStepsPanel(QWidget):
    """El panel que lista las transacciones de la operación en curso.

    Nace oculto y sólo aparece cuando hay algo que contar. `start` lo deja listo
    para una operación nueva —vacío y visible— y `record` añade o actualiza la
    fila del paso que llega: como cada paso ocurre una sola vez por ejecución,
    la clave del diccionario es el propio paso.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chain = ""
        self._rows: dict[SwapStep, _StepRow] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(4)

        self._titulo = QLabel("TRANSACCIONES DE LA OPERACIÓN")
        self._titulo.setObjectName("sectionTitle")
        lay.addWidget(self._titulo)

        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(3)
        lay.addLayout(self._list)

        self.setVisible(False)

    def start(self, chain_key: str) -> None:
        """Deja el panel listo para una operación nueva, en esa red.

        La red se fija aquí y no en cada aviso porque es la de la cotización que
        se está ejecutando, y es lo que hace falta para enlazar cada hash a su
        explorador.
        """
        self.clear()
        self._chain = chain_key
        self.setVisible(True)

    def record(self, update: StepUpdate) -> None:
        """Pinta un aviso de progreso. Crea la fila del paso si no estaba."""
        row = self._rows.get(update.step)
        if row is None:
            row = _StepRow(self)
            self._rows[update.step] = row
            self._list.addWidget(row)
        row.apply(update, self._chain)
        self.setVisible(True)

    def clear(self) -> None:
        """Vacía la lista y esconde el panel: el estado inicial de siempre."""
        while self._list.count():
            item = self._list.takeAt(0)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        self.setVisible(False)

    @property
    def title_label(self) -> QLabel:
        """El rótulo del panel, para las pruebas."""
        return self._titulo

    def title_of(self, step: SwapStep) -> str:
        """El título vigente de un paso, o «» si ese paso no llegó a empezar."""
        row = self._rows.get(step)
        return "" if row is None else row.title_text()

    def glyph_of(self, step: SwapStep) -> str:
        """El glifo vigente de un paso, o «» si ese paso no llegó a empezar."""
        row = self._rows.get(step)
        return "" if row is None else row.glyph_text()

    def steps(self) -> tuple[SwapStep, ...]:
        """Los pasos con fila, en el orden en que ocurrieron."""
        return tuple(self._rows)
