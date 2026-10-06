"""Pestaña: Mercados de predicción."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.analyze_prediction_market import MarketReport
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import spawn


class PredictionPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar en la pregunta… (vacío = todos)")
        top.addWidget(self._search, stretch=1)
        self._btn = QPushButton("Buscar")
        self._btn.clicked.connect(self._on_search)
        top.addWidget(self._btn)
        lay.addLayout(top)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["Pregunta", "Favorito", "Total %", "Overround", "Coherente", "Cierre"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(True)
        lay.addWidget(self._table, stretch=1)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._detail)

        self._table.itemSelectionChanged.connect(self._on_select)
        self._reports: tuple[MarketReport, ...] = ()

    def _on_search(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando Polymarket…")
        spawn(self._do_search())

    async def _do_search(self) -> None:
        try:
            text = self._search.text().strip() or None
            reports = await self._container.analyze_markets(limit=30, search=text)
            self._reports = reports
            self._fill(reports)
            self._status.setText(f"{len(reports)} mercado(s) · lo incoherente primero")
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._table.setRowCount(0)
        finally:
            self._btn.setEnabled(True)

    def _fill(self, reports: tuple[MarketReport, ...]) -> None:
        self._table.setRowCount(0)
        for rep in reports:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(rep.question))
            self._table.setItem(row, 1, QTableWidgetItem(f"{rep.favourite.label} ({rep.favourite.implied_percent:.1f} %)"))
            self._table.setItem(row, 2, QTableWidgetItem(f"{rep.total_percent:.2f} %"))
            self._table.setItem(row, 3, QTableWidgetItem(str(rep.overround_bps)))
            self._table.setItem(row, 4, QTableWidgetItem("✓" if rep.is_coherent else "✗"))
            closes = rep.market.closes_at.isoformat() if rep.market.closes_at else "—"
            self._table.setItem(row, 5, QTableWidgetItem(closes))
        self._table.resizeRowsToContents()

    def _on_select(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or not self._reports:
            return
        rep = self._reports[rows[0].row()]
        outcomes = "  |  ".join(f"{o.label}: {o.implied_percent:.1f} %" for o in rep.market.outcomes)
        self._detail.setText(f"{rep.note}  —  {outcomes}")
