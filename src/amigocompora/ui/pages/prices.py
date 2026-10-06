"""Pestaña: Cotizaciones DEX y oportunidades."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.models import Opportunity, PriceComparison
from amigocompora.engines.catalog import quote_token, tokens_for
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import spawn


class PricesPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: PriceComparison | None = None

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # Filtros
        top = QHBoxLayout()
        top.addWidget(QLabel("Red:"))
        self._chain = QComboBox()
        for key in sorted(CHAINS):
            self._chain.addItem(f"{CHAINS[key].name} ({key})", key)
        top.addWidget(self._chain)

        self._base = QComboBox()
        top.addWidget(QLabel("Base:"))
        top.addWidget(self._base)
        self._quote = QLabel("—")
        top.addWidget(QLabel("Quote:"))
        top.addWidget(self._quote)

        top.addWidget(QLabel("Cantidad:"))
        self._amount = QDoubleSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        top.addWidget(self._amount)

        self._btn = QPushButton("Cotizar")
        self._btn.clicked.connect(self._on_quote)
        top.addWidget(self._btn)
        top.addStretch()
        lay.addLayout(top)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(["Venue", "Recibes", "Precio", "Comisión", "Impacto", "Liquidez", "Nota"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._table, stretch=1)

        self._opp_table = QTableWidget(0, 5)
        self._opp_table.setHorizontalHeaderLabels(["Mejor", "Referencia", "Spread bruto", "Spread neto", "Accionable"])
        self._opp_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._opp_table.setAlternatingRowColors(True)
        self._opp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(QLabel("<b>Oportunidades (diferencial neto ≥ 10 bps)</b>"))
        lay.addWidget(self._opp_table)

        self._chain.currentIndexChanged.connect(self._refresh_tokens)
        self._refresh_tokens()

    def _refresh_tokens(self) -> None:
        key = self._chain.currentData()
        self._base.clear()
        for token in tokens_for(key):
            # Sólo bases que no sean la quote.
            qt = quote_token(key)
            if qt is not None and token.symbol == qt.symbol:
                continue
            self._base.addItem(f"{token.symbol}", token)
        qt = quote_token(key)
        self._quote.setText(qt.symbol if qt is not None else "—")

    def _on_quote(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando…")
        spawn(self._do_quote())

    async def _do_quote(self) -> None:
        try:
            chain_key: str = self._chain.currentData()
            base_token = self._base.currentData()
            if base_token is None:
                self._status.setText("Selecciona un token base.")
                return
            qt = quote_token(chain_key)
            if qt is None:
                self._status.setText("Esta red no tiene stablecoin de referencia.")
                return
            from amigocompora.domain.models import TradingPair

            pair = TradingPair(base=base_token, quote=qt)
            amount = base_token.amount(Decimal(str(self._amount.value())))
            comparison = await self._container.compare_prices(pair, amount)
            self._comparison = comparison
            self._fill_table(comparison)
            # También buscar oportunidades con el umbral por defecto.
            opps = await self._container.scan_opportunities(pair, amount)
            self._fill_opps(opps)
            note = "con estimaciones" if comparison.has_estimates else "todo medido"
            if comparison.has_unknown_fees:
                note += " · hay comisiones no desglosadas"
            self._status.setText(
                f"{len(comparison.quotes)} venue(s) · spread {comparison.spread_bps} · {note} · observado {comparison.observed_at.isoformat()}"
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._table.setRowCount(0)
            self._opp_table.setRowCount(0)
        finally:
            self._btn.setEnabled(True)

    def _fill_table(self, comp: PriceComparison) -> None:
        self._table.setRowCount(0)
        for quote in comp.ranked:
            row = self._table.rowCount()
            self._table.insertRow(row)
            fee = str(quote.fee_bps) if quote.fee_bps is not None else "— no desglosada"
            fee_basis = quote.fee_basis.value if quote.fee_basis is not None else "—"
            self._table.setItem(row, 0, QTableWidgetItem(quote.venue.name))
            self._table.setItem(row, 1, QTableWidgetItem(str(quote.amount_out)))
            self._table.setItem(row, 2, QTableWidgetItem(str(quote.price)))
            item_fee = QTableWidgetItem(f"{fee} [{fee_basis}]")
            if not quote.fee_is_known:
                item_fee.setForeground(Qt.yellow)
            self._table.setItem(row, 3, item_fee)
            item_imp = QTableWidgetItem(f"{quote.price_impact_bps} [{quote.impact_basis.value}]")
            if not quote.is_exact:
                item_imp.setForeground(Qt.yellow)
            self._table.setItem(row, 4, item_imp)
            self._table.setItem(row, 5, QTableWidgetItem(str(quote.liquidity) if quote.liquidity else "—"))
            self._table.setItem(row, 6, QTableWidgetItem(quote.source_note))

    def _fill_opps(self, opps: tuple[Opportunity, ...]) -> None:
        self._opp_table.setRowCount(0)
        for opp in opps:
            row = self._opp_table.rowCount()
            self._opp_table.insertRow(row)
            self._opp_table.setItem(row, 0, QTableWidgetItem(opp.best.venue.name))
            self._opp_table.setItem(row, 1, QTableWidgetItem(opp.reference.venue.name))
            self._opp_table.setItem(row, 2, QTableWidgetItem(str(opp.gross_spread_bps)))
            self._opp_table.setItem(row, 3, QTableWidgetItem(str(opp.net_spread_bps)))
            mark = "✓" if opp.is_actionable else "—"
            self._opp_table.setItem(row, 4, QTableWidgetItem(mark))
