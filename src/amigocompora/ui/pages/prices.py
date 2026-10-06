"""Pestaña: Cotizaciones DEX y oportunidades."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.addresses import is_evm_address, is_solana_address
from amigocompora.domain.chains import CHAINS, AddressFormat, chain
from amigocompora.domain.errors import ConfirmationDeniedError
from amigocompora.domain.models import Opportunity, PlannedTransaction, PriceComparison, Quote
from amigocompora.engines.catalog import quote_token, tokens_for
from amigocompora.ui.theme import COLOR_MUTED
from amigocompora.ui.widgets import spawn

#: Aviso que acompaña a todo payload exportado. Va dentro del fichero y no sólo
#: en la pantalla: el fichero sobrevive a la sesión y viaja a otro sitio, y quien
#: lo abra tiene que saber que no es una transacción emitida.
_UNSIGNED_NOTICE = (
    "Transacción SIN FIRMAR. Amigocompora no firma ni emite transacciones. "
    "Revísala y fírmala con tu propia cartera."
)


class PricesPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: PriceComparison | None = None
        self._prepared: PlannedTransaction | None = None

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
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Venue", "Motor", "Recibes", "Precio", "Comisión", "Impacto", "Liquidez", "Nota"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
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

        # Acciones sobre la cotización elegida. Deshabilitadas hasta que haya una
        # fila seleccionada y un motor capaz de construir: un botón que existe y
        # falla al pulsarlo enseña a desconfiar de los botones.
        actions = QHBoxLayout()
        self._swap_btn = QPushButton("Preparar swap…")
        self._swap_btn.setObjectName("secondary")
        self._swap_btn.setToolTip(
            "Construye la transacción sin firmar de la cotización seleccionada. "
            "Amigocompora no la firma ni la emite."
        )
        self._swap_btn.clicked.connect(self._on_prepare_swap)
        self._swap_btn.setEnabled(False)
        actions.addWidget(self._swap_btn)

        self._save_btn = QPushButton("Guardar payload…")
        self._save_btn.setObjectName("secondary")
        self._save_btn.setToolTip("Exporta a JSON la última transacción sin firmar que confirmaste.")
        self._save_btn.clicked.connect(self._on_save_payload)
        self._save_btn.setEnabled(False)
        actions.addWidget(self._save_btn)
        actions.addStretch()
        lay.addLayout(actions)

        self._table.itemSelectionChanged.connect(self._on_quote_selected)
        self._chain.currentIndexChanged.connect(self._refresh_tokens)
        self._refresh_tokens()

    # ------------------------------------------------------------------ #
    # Filtros
    # ------------------------------------------------------------------ #
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
            if comparison.has_partial_sources:
                # Se dice en la misma línea que el resultado y no en un aviso
                # aparte: una comparación a la que le falta una fuente se lee
                # como «aquí no hay nada mejor», y puede que la mejor fuera
                # justo la que no respondió.
                note += f" · sin respuesta: {', '.join(comparison.failed_engines)}"
            self._status.setText(
                f"{len(comparison.quotes)} venue(s) · spread {comparison.spread_bps} · {note} · observado {comparison.observed_at.isoformat()}"
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._table.setRowCount(0)
            self._opp_table.setRowCount(0)
        finally:
            self._btn.setEnabled(True)
            self._on_quote_selected()

    def _fill_table(self, comp: PriceComparison) -> None:
        self._table.setRowCount(0)
        for quote in comp.ranked:
            row = self._table.rowCount()
            self._table.insertRow(row)
            fee = str(quote.fee_bps) if quote.fee_bps is not None else "— no desglosada"
            fee_basis = quote.fee_basis.value if quote.fee_basis is not None else "—"
            self._table.setItem(row, 0, QTableWidgetItem(quote.venue.name))
            # Qué motor produjo la cifra. Con varios motores activos la tabla es
            # una mezcla, y dos motores pueden cotizar el mismo par por caminos
            # distintos: sin esta columna, dos filas del mismo venue parecerían
            # un error de la vista en vez de dos fuentes que no coinciden.
            self._table.setItem(row, 1, QTableWidgetItem(quote.engine_id))
            self._table.setItem(row, 2, QTableWidgetItem(str(quote.amount_out)))
            self._table.setItem(row, 3, QTableWidgetItem(str(quote.price)))
            item_fee = QTableWidgetItem(f"{fee} [{fee_basis}]")
            if not quote.fee_is_known:
                item_fee.setForeground(Qt.yellow)
            self._table.setItem(row, 4, item_fee)
            # Un impacto sin publicar no es un cero: se escribe igual que en el
            # diálogo de confirmación, para que las dos vistas digan lo mismo de
            # la misma cotización.
            impact = (
                str(quote.price_impact_bps)
                if quote.price_impact_bps is not None
                else "— no publicado"
            )
            impact_basis = quote.impact_basis.value if quote.impact_basis is not None else "—"
            item_imp = QTableWidgetItem(f"{impact} [{impact_basis}]")
            if not quote.impact_is_known or not quote.is_exact:
                item_imp.setForeground(Qt.yellow)
            self._table.setItem(row, 5, item_imp)
            self._table.setItem(
                row, 6, QTableWidgetItem(str(quote.liquidity) if quote.liquidity else "—")
            )
            self._table.setItem(row, 7, QTableWidgetItem(quote.source_note))

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

    # ------------------------------------------------------------------ #
    # Preparar el swap
    # ------------------------------------------------------------------ #
    def _selected_quote(self) -> Quote | None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or self._comparison is None:
            return None
        ranked = self._comparison.ranked
        index = rows[0].row()
        return ranked[index] if 0 <= index < len(ranked) else None

    def _on_quote_selected(self) -> None:
        self._swap_btn.setEnabled(
            self._selected_quote() is not None and self._container.prepare_swap.is_available()
        )

    def _recipient_candidates(self, chain_key: str) -> list[str]:
        """Las direcciones ya configuradas que son válidas en esa red.

        Ofrecer las propias antes que un campo vacío no es comodidad: teclear una
        dirección a mano es la forma más común de perder fondos, y la lista sale
        de lo que el usuario ya declaró en su configuración.
        """
        spec = chain(chain_key)
        solana = spec.address_format is AddressFormat.SOLANA_BASE58
        check = is_solana_address if solana else is_evm_address
        return [address for address in self._container.settings.watch_addresses if check(address)]

    def _ask_recipient(self, chain_key: str) -> str | None:
        candidates = self._recipient_candidates(chain_key)
        title = "Destino del swap"
        if candidates:
            text, accepted = QInputDialog.getItem(
                self,
                title,
                "Dirección que recibe (elige una tuya o pega otra):",
                candidates,
                0,
                True,  # editable: la lista es un atajo, no un límite
            )
        else:
            text, accepted = QInputDialog.getText(self, title, "Dirección que recibe:")
        if not accepted:
            return None
        return text.strip() or None

    def _on_prepare_swap(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una cotización de la tabla.")
            return
        recipient = self._ask_recipient(quote.pair.chain)
        if recipient is None:
            return
        self._swap_btn.setEnabled(False)
        self._status.setText("Construyendo la transacción sin firmar…")
        spawn(self._do_prepare(quote, recipient))

    async def _do_prepare(self, quote: Quote, recipient: str) -> None:
        try:
            transaction = await self._container.prepare_swap(quote, recipient=recipient)
            self._prepared = transaction
            self._save_btn.setEnabled(True)
            self._status.setText(
                "Transacción preparada y confirmada. Amigocompora NO la ha firmado "
                "ni emitido: guárdala y fírmala en tu cartera."
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._status.setText("Cancelaste la preparación. No se construyó nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
        finally:
            self._on_quote_selected()

    def _on_save_payload(self) -> None:
        if self._prepared is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar transacción sin firmar",
            "swap-sin-firmar.json",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(_payload_document(self._prepared), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as error:
            self._status.setText(f"No se pudo guardar: {error}")
            return
        self._status.setText(
            f"Guardada en {path}. Sigue SIN FIRMAR: nada se ha emitido."
        )


def _payload_document(transaction: PlannedTransaction) -> dict[str, str]:
    """Documento exportable de un payload sin firmar.

    Se construye desde `describe()` —los mismos campos que se mostraron en el
    diálogo de confirmación— y no volcando el objeto: lo que se exporta tiene que
    ser exactamente lo que el usuario leyó y aprobó, no una representación
    interna que puede cambiar sin que el diálogo cambie.
    """
    document: dict[str, str] = {"tipo": type(transaction).__name__}
    document.update(dict(transaction.describe()))
    document["aviso"] = _UNSIGNED_NOTICE
    return document
