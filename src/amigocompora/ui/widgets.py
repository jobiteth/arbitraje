"""Widgets reutilizables de la UI."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

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
from amigocompora.domain.models import UnsignedTransaction
from amigocompora.ui.theme import COLOR_BORDER, COLOR_CARD, COLOR_MUTED

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

        warning = QLabel("Amigocompora <b>nunca firma ni emite</b> transacciones. Revisa los datos antes de confirmar.")
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #f5c518; font-size: 11px;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("Confirmar")
        buttons.button(QDialogButtonBox.No).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _tx_widget(tx: UnsignedTransaction) -> QWidget:
        box = QFrame()
        box.setStyleSheet(f"background: #0f1115; border: 1px solid {COLOR_BORDER}; border-radius: 6px;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)
        for label, value in (
            ("Cadena (EIP-155)", str(tx.chain_id)),
            ("Destino", tx.to_address),
            ("Valor", str(tx.value)),
            ("Descripción", tx.description),
            ("Calldata", (tx.calldata[:120] + "…") if len(tx.calldata) > 120 else tx.calldata),
        ):
            row = QLabel(f"<span style='color:{COLOR_MUTED}'>{label}:</span> {value}")
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
