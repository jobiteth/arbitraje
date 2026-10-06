"""Widgets reutilizables de la UI."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.confirmation import PendingAction
from amigocompora.domain.models import PlannedTransaction
from amigocompora.domain.modes import Capability
from amigocompora.ui.theme import (
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_WARNING,
)

#: Un payload en base64 o un calldata ocupan cientos de caracteres y no caben en
#: el diálogo. Se recorta sólo para **mostrar**: el valor completo sigue en el
#: objeto, que es lo que se exporta.
_CLIP_CHARS: Final = 120


def _clip(value: str) -> str:
    return value if len(value) <= _CLIP_CHARS else f"{value[:_CLIP_CHARS]}…"


#: El aviso del diálogo, según lo que la acción va a hacer **de verdad**.
#:
#: Estaba escrito a mano —«Amigocompora nunca firma ni emite transacciones»— y
#: con la ejecución real esa frase pasó a ser falsa. Es la línea que más importa
#: de todo el diálogo, porque dice justo lo contrario de lo que va a ocurrir en
#: el instante exacto en que alguien decide si ocurre. La capacidad viaja dentro
#: de la acción, así que el aviso puede depender de ella en vez de suponerla.
#:
#: Sólo hay dos textos porque sólo hay dos capacidades confirmables
#: (`CONFIRMABLE_CAPABILITIES`, en `domain/modes.py`): construir el payload y
#: emitirlo. No hay una tercera rama esperando, y por eso no hay un `else` que
#: adivine.
_SIGNING_WARNING: Final = (
    "Esta operación es <b>real e irreversible</b>. Amigocompora va a <b>firmar y "
    "emitir</b> una transacción a la red: sale dinero de tu cartera y, una vez "
    "emitida, no se puede deshacer ni cancelar. Revisa la red, el importe y el "
    "destino."
)

_PREPARE_WARNING: Final = (
    "Confirmar aquí sólo autoriza a <b>construir</b> el payload. En esta acción "
    "Amigocompora no firma ni emite nada."
)


def warning_for(capability: Capability) -> tuple[str, str]:
    """El texto del aviso y su color, para la capacidad que se está confirmando."""
    if capability is Capability.BROADCAST_TX:
        return _SIGNING_WARNING, COLOR_DANGER
    return _PREPARE_WARNING, COLOR_WARNING

#: Tareas de fondo vivas. Guardar la referencia evita que el recolector de
#: basura cancele una corrutina recién lanzada desde un handler de Qt.
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Lanza una corrutina desde la UI y conserva su referencia hasta que acaba."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class Card(QFrame):
    """Contenedor con borde y fondo de tarjeta."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"Card {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}"
        )


class Badge(QLabel):
    """Etiqueta pequeña coloreada (severidad, estado)."""

    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setStyleSheet(
            f"background: {color}; color: white; border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600;"
        )
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(18)


class ConfirmationDialog(QDialog):
    """Diálogo que implementa `ConfirmationPrompt`.

    Muestra título, detalles y, si existe, el payload de la transacción sin firmar.
    """

    def __init__(
        self,
        action: PendingAction,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirmar acción — Amigocompora")
        self.setModal(True)
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(f"<b>{action.title}</b>")
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)
        layout.addWidget(title)

        if action.details:
            details = QLabel("<br/>".join(f"• {d}" for d in action.details))
            details.setWordWrap(True)
            details.setStyleSheet(f"color: {COLOR_MUTED};")
            layout.addWidget(details)

        if action.transaction is not None:
            layout.addWidget(self._tx_widget(action.transaction))

        warning = QLabel()
        warning.setTextFormat(Qt.RichText)
        warning.setWordWrap(True)
        texto, color = warning_for(action.capability)
        warning.setText(texto)
        warning.setStyleSheet(f"color: {color}; font-size: 11px;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("Confirmar")
        buttons.button(QDialogButtonBox.No).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _tx_widget(tx: PlannedTransaction) -> QWidget:
        box = QFrame()
        box.setStyleSheet(f"background: #0f1115; border: 1px solid {COLOR_BORDER}; border-radius: 6px;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)
        # La vista no sabe de qué red es el payload: cada tipo se describe a sí
        # mismo y aquí sólo se pinta. Añadir una red nueva no toca este diálogo.
        for label, value in tx.describe():
            row = QLabel(f"<span style='color:{COLOR_MUTED}'>{label}:</span> {_clip(value)}")
            row.setTextFormat(Qt.RichText)
            row.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.setWordWrap(True)
            lay.addWidget(row)
        return box


class QtConfirmationPrompt:
    """Adaptador `ConfirmationPrompt` que abre `ConfirmationDialog` en el hilo de Qt.

    Se instala en `ConfirmationGateway` desde `MainWindow`.
    """

    def __init__(self, parent: QWidget) -> None:
        self._parent = parent

    async def ask(self, action: PendingAction) -> bool:
        # qasync hace que este `await` ceda al event loop de Qt sin bloquear.
        dialog = ConfirmationDialog(action, self._parent)
        result = dialog.exec()
        return result == QDialog.Accepted


class AlertBanner(QWidget):
    """Banner superior que muestra la última alerta no reconocida."""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        lay.addWidget(self._label, stretch=1)
        btn = QPushButton("Descartar")
        btn.setObjectName("secondary")
        btn.clicked.connect(self._on_dismiss)
        lay.addWidget(btn)
        self.setStyleSheet("AlertBanner { background: #2a2410; border: 1px solid #665500; border-radius: 6px; }")

    def show_alert(self, title: str, detail: str, severity: str) -> None:
        color = {"low": "#8b95a5", "medium": "#f5c518", "high": "#ff5a5a"}.get(severity, "#8b95a5")
        self._label.setText(f"<span style='color:{color}; font-weight:700'>● {severity.upper()}</span>  <b>{title}</b> — {detail}")
        self._label.setTextFormat(Qt.RichText)
        self.setVisible(True)

    def _on_dismiss(self) -> None:
        self.setVisible(False)
        self.dismissed.emit()


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    # Mostrar en hora local del usuario.
    try:
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt.isoformat()
