"""Pestaña: Alertas / bandeja."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.alerts import AlertCenter
from amigocompora.ui.theme import COLOR_MUTED, SEVERITY_COLOR


class AlertsPage(QWidget):
    def __init__(self, center: AlertCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._center = center
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self._count = QLabel("")
        self._count.setStyleSheet(f"color: {COLOR_MUTED};")
        top.addWidget(self._count)
        top.addStretch()
        btn_ack = QPushButton("Marcar todo como leído")
        btn_ack.setObjectName("secondary")
        btn_ack.clicked.connect(self._ack_all)
        top.addWidget(btn_ack)
        btn_clear = QPushButton("Vaciar")
        btn_clear.setObjectName("secondary")
        btn_clear.clicked.connect(self._clear)
        top.addWidget(btn_clear)
        btn_refresh = QPushButton("Refrescar")
        btn_refresh.setObjectName("secondary")
        btn_refresh.clicked.connect(self._refresh)
        top.addWidget(btn_refresh)
        lay.addLayout(top)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["Severidad", "Tipo", "Título", "Detalle", "Visto", "Estado"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._table, stretch=1)

        center.subscribe(lambda _a: self._refresh())
        self._refresh()

    def _refresh(self) -> None:
        alerts = self._center.alerts()
        self._count.setText(f"{len(alerts)} alerta(s) · {self._center.unacknowledged_count} sin leer")
        self._table.setRowCount(0)
        for alert in alerts:
            row = self._table.rowCount()
            self._table.insertRow(row)
            color = SEVERITY_COLOR.get(alert.severity.value, COLOR_MUTED)
            sev_item = QTableWidgetItem(alert.severity.value.upper())
            sev_item.setForeground(color)  # type: ignore[arg-type]
            self._table.setItem(row, 0, sev_item)
            self._table.setItem(row, 1, QTableWidgetItem(alert.kind.value))
            self._table.setItem(row, 2, QTableWidgetItem(alert.title))
            self._table.setItem(row, 3, QTableWidgetItem(alert.detail[:160]))
            times = f"{alert.last_seen_at.isoformat()} (×{alert.count})" if alert.count > 1 else alert.last_seen_at.isoformat()
            self._table.setItem(row, 4, QTableWidgetItem(times))
            self._table.setItem(row, 5, QTableWidgetItem("✓ leída" if alert.acknowledged else "● nueva"))

    def _ack_all(self) -> None:
        self._center.acknowledge_all()
        self._refresh()

    def _clear(self) -> None:
        self._center.clear()
        self._refresh()
