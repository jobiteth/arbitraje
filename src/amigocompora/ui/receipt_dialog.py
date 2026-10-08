"""La confirmación de una operación emitida: qué pasó, con qué hash, y dónde verlo.

### Por qué es una ventana y no sólo la línea de estado

Tras firmar y emitir, el resultado tiene que verse sin buscarlo. La línea de estado
de la tarjeta queda debajo de los importes y no se lee; y un dinero que sale de la
cartera merece algo más que una frase en letra pequeña. La ventana dice el estado
con palabras propias de cada caso, enseña el hash entero para poder comprobarlo, y
da un enlace al explorador de la red cuando lo conocemos.

### Qué no hace

No decide nada: sólo enseña lo que devolvió el emisor. El estado sale de
`BroadcastStatus` y se traduce aquí; no se reinterpreta.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.domain.models import BroadcastReceipt, BroadcastStatus, Quote
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_SUCCESS, COLOR_WARNING

#: Dónde se ve una transacción en el explorador de cada red. Sólo las que tienen uno
#: conocido; para el resto la ventana enseña el hash sin enlace en vez de inventarlo.
EXPLORER_TX_URL: dict[str, str] = {
    "ethereum": "https://etherscan.io/tx/{tx}",
    "base": "https://basescan.org/tx/{tx}",
    "polygon": "https://polygonscan.com/tx/{tx}",
    "arbitrum": "https://arbiscan.io/tx/{tx}",
    "optimism": "https://optimistic.etherscan.io/tx/{tx}",
    "bsc": "https://bscscan.com/tx/{tx}",
    "avalanche": "https://snowtrace.io/tx/{tx}",
}


def explorer_url(chain_key: str, tx_hash: str) -> str | None:
    """El enlace al explorador de esa transacción, o `None` si la red no tiene uno."""
    plantilla = EXPLORER_TX_URL.get(chain_key)
    return plantilla.format(tx=tx_hash) if plantilla else None


def _titulo_y_color(status: BroadcastStatus) -> tuple[str, str, str]:
    """El título, la explicación y el color de cada estado. Cada uno dice lo suyo."""
    if status is BroadcastStatus.SUCCESS:
        return (
            "Operación confirmada",
            "La red la incluyó en un bloque y el swap se ejecutó.",
            COLOR_SUCCESS,
        )
    if status is BroadcastStatus.PENDING:
        return (
            "Operación enviada, pendiente de confirmar",
            "La transacción está en la red y aún no se ha minado. El dinero ya salió "
            "de tu cartera: comprueba el hash en el explorador hasta que aparezca "
            "confirmada.",
            COLOR_WARNING,
        )
    if status is BroadcastStatus.REVERTED:
        return (
            "La operación revirtió",
            "La red la procesó y falló: se pagó el gas, pero el swap no ocurrió y el "
            "importe sigue en tu cartera.",
            COLOR_DANGER,
        )
    return (
        "No se sabe si la operación salió",
        "Ningún nodo confirmó la recepción. Puede que la transacción esté viva: "
        "comprueba el hash antes de intentarlo otra vez, para no pagarla dos veces.",
        COLOR_WARNING,
    )


class SwapReceiptDialog(QDialog):
    """Lo que pasó con una operación emitida, con su hash y su enlace."""

    def __init__(
        self,
        *,
        quote: Quote,
        receipt: BroadcastReceipt,
        recipient: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Operación emitida — Amigocompora")
        self.setModal(False)
        self.setMinimumWidth(560)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        titulo, explicacion, color = _titulo_y_color(receipt.status)
        cabecera = QLabel(f"<b style='font-size:15px'>{titulo}</b>")
        cabecera.setTextFormat(Qt.RichText)
        cabecera.setStyleSheet(f"color: {color};")
        lay.addWidget(cabecera)

        detalle = QLabel(explicacion)
        detalle.setWordWrap(True)
        lay.addWidget(detalle)

        # El resumen de lo que se hizo, en las mismas unidades que se firmaron.
        resumen = (
            f"<b>Red:</b> {quote.pair.chain}<br/>"
            f"<b>Entregaste:</b> {quote.amount_in}<br/>"
            f"<b>Recibes (estimado):</b> {quote.amount_out}<br/>"
            f"<b>Motor:</b> {quote.engine_id} · {quote.venue.name}<br/>"
            f"<b>Destino de los fondos:</b> {recipient}"
        )
        datos = QLabel(resumen)
        datos.setTextFormat(Qt.RichText)
        datos.setWordWrap(True)
        datos.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(datos)

        # El hash entero, seleccionable y con botón de copiar: es lo que se pega en un
        # explorador o en un mensaje de soporte, y acortarlo lo hace inútil.
        lay.addWidget(QLabel("<b>Hash de la transacción</b>"))
        fila = QHBoxLayout()
        self._hash = QLineEdit(receipt.tx_hash)
        self._hash.setReadOnly(True)
        self._hash.setCursorPosition(0)
        self._hash.setStyleSheet("font-family: 'Consolas', monospace;")
        fila.addWidget(self._hash, 1)
        copiar = QPushButton("Copiar")
        copiar.setObjectName("secondary")
        copiar.clicked.connect(self._copiar)
        fila.addWidget(copiar)
        lay.addLayout(fila)

        self._aviso_copia = QLabel("")
        self._aviso_copia.setObjectName("hint")
        lay.addWidget(self._aviso_copia)

        enlace = explorer_url(quote.pair.chain, receipt.tx_hash)
        if enlace is not None:
            ver = QLabel(f"<a href='{enlace}'>Ver la transacción en el explorador</a>")
            ver.setTextFormat(Qt.RichText)
            ver.setOpenExternalLinks(True)
            lay.addWidget(ver)
        else:
            sin = QLabel(
                "No hay explorador configurado para esta red: busca el hash en el "
                "explorador que uses."
            )
            sin.setStyleSheet(f"color: {COLOR_MUTED};")
            sin.setWordWrap(True)
            lay.addWidget(sin)

        if receipt.reason:
            motivo = QLabel(f"<span style='color:{COLOR_MUTED}'>{receipt.reason}</span>")
            motivo.setTextFormat(Qt.RichText)
            motivo.setWordWrap(True)
            lay.addWidget(motivo)

        if receipt.block_number is not None:
            bloque = QLabel(f"Bloque {receipt.block_number}")
            bloque.setStyleSheet(f"color: {COLOR_MUTED};")
            lay.addWidget(bloque)

        momento = _formatear(receipt.observed_at)
        hora = QLabel(f"Observada a las {momento}")
        hora.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(hora)

        caja = QDialogButtonBox(QDialogButtonBox.Close)
        caja.rejected.connect(self.reject)
        caja.accepted.connect(self.accept)
        lay.addWidget(caja)

    def _copiar(self) -> None:
        QApplication.clipboard().setText(self._hash.text())
        self._aviso_copia.setText("Hash copiado al portapapeles.")


def _formatear(momento: datetime) -> str:
    """La hora local, como se lee en la pantalla, sin zona horaria visible."""
    return momento.astimezone().strftime("%H:%M:%S")
