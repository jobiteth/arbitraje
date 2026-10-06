"""Pestaña: Motores (hot-swap)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.protocols import EngineKind
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import spawn


class EnginesPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["ID", "Nombre", "Ranura", "Versión", "Origen", "Activo"])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._table, stretch=1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Activar:"))
        self._combo = QComboBox()
        row.addWidget(self._combo, stretch=1)
        self._activate_btn = QPushButton("Activar")
        self._activate_btn.clicked.connect(self._on_activate)
        row.addWidget(self._activate_btn)
        lay.addLayout(row)

        refresh_btn = QPushButton("Refrescar")
        refresh_btn.setObjectName("secondary")
        refresh_btn.clicked.connect(self._refresh)
        lay.addWidget(refresh_btn)

        self._refresh()

    def _refresh(self) -> None:
        reg = self._container.registry
        catalog = reg.available()
        self._table.setRowCount(0)
        self._combo.clear()
        for entry in catalog:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(entry.engine_id))
            self._table.setItem(r, 1, QTableWidgetItem(entry.manifest.name))
            self._table.setItem(r, 2, QTableWidgetItem(entry.manifest.kind.value))
            self._table.setItem(r, 3, QTableWidgetItem(entry.manifest.version))
            self._table.setItem(r, 4, QTableWidgetItem(entry.source))
            active = "✓" if reg.is_active(entry.engine_id) else "—"
            self._table.setItem(r, 5, QTableWidgetItem(active))
            blocked = entry.blocked_capabilities(self._container.guard)
            if blocked:
                names = ", ".join(sorted(cap.value for cap in blocked))
                hint = f"bloqueadas en {self._container.guard.mode.label}: {names}"
            else:
                hint = "todas las capacidades disponibles"
            id_item = self._table.item(r, 0)
            if id_item is not None:
                id_item.setToolTip(hint)
            self._combo.addItem(f"{entry.manifest.name} ({entry.engine_id})", entry.engine_id)

        active_map = {k.value: (v.manifest.engine_id if v else "—") for k, v in ((k, reg.active_or_none(k)) for k in EngineKind)}
        self._status.setText("Activos: " + "  ·  ".join(f"{k}={v}" for k, v in active_map.items()))

    def _on_activate(self) -> None:
        engine_id = self._combo.currentData()
        if not engine_id:
            return
        self._activate_btn.setEnabled(False)
        self._status.setText(f"Activando {engine_id}…")
        spawn(self._do_activate(engine_id))

    async def _do_activate(self, engine_id: str) -> None:
        try:
            await self._container.registry.activate(engine_id)
            self._status.setText(f"«{engine_id}» activo.")
        except Exception as error:
            self._status.setText(f"Error al activar «{engine_id}»: {error}")
        finally:
            self._activate_btn.setEnabled(True)
            self._refresh()
