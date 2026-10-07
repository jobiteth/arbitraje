"""Pestaña: Mercados de predicción.

Dos tablas sobre los **mismos** datos: la de mercados muestra la lectura de cada
uno, y la de cestas deriva de ella la única condición de arbitraje que se puede
afirmar sin un modelo de valoración externo — que comprar todos los resultados
cueste menos que el pago garantizado de 1.

Los dos umbrales son controles de la vista, no de la consulta: ni el de margen ni
el de ventana vuelven a pedir nada a la fuente. El de margen recalcula las cestas
sobre los informes ya traídos (`FindPredictionOpportunities.from_reports`) y el de
ventana recorta la lista de mercados ya traída. Es la razón por la que esos
métodos están separados, y aquí es donde se aprovechan.

La ventana, además, **se pide** en la siguiente búsqueda: con «24 h» el motor
ordena por fecha de cierre y filtra en el servidor, para que los 30 mercados que
se traen sean los que cierran pronto y no los 30 de más volumen de los cuales sólo
tres cierran mañana. Recortar en cliente lo ya traído no basta para eso, y por eso
se hacen las dos cosas.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.analyze_prediction_market import MarketReport
from amigocompora.app.usecases.find_prediction_opportunities import (
    DEFAULT_MIN_EDGE_BPS,
    BasketOpportunity,
)
from amigocompora.domain.money import BasisPoints
from amigocompora.ui.theme import COLOR_MUTED, COLOR_SUCCESS
from amigocompora.ui.widgets import spawn

#: Ventanas de cierre que ofrece el selector, de la más corta a la más larga.
#: `None` es «todas», que es la vista por volumen de siempre.
_WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("todas", None),
    ("24 h", timedelta(hours=24)),
    ("7 días", timedelta(days=7)),
    ("30 días", timedelta(days=30)),
)

#: Aviso permanente bajo la tabla de cestas. El margen que se muestra es bruto y
#: el caso de uso lo documenta en detalle; esto es lo que el usuario tiene que
#: leer antes de sacar una conclusión de la cifra.
_BASKET_CAVEAT = (
    "Una cesta es una participación de <b>cada</b> resultado, así que paga 1 pase "
    "lo que pase. El margen es <b>bruto</b>: no descuenta gas ni comisiones de la "
    "plataforma, no comprueba que haya profundidad a esos precios, y las patas hay "
    "que ejecutarlas a la vez. Es una desviación a revisar, no una orden."
)


def countdown(closes_at: datetime | None, now: datetime) -> str:
    """Cuánto falta para el cierre, en una unidad y sin decimales.

    Una fecha absoluta obliga a restarla mentalmente contra el reloj, y con 30
    filas eso no se hace: se lee. La unidad se elige para que quepa de un vistazo
    —minutos si falta menos de una hora, horas si falta menos de dos días, días a
    partir de ahí— y lo que sobra se trunca hacia abajo, porque decir «en 2 días»
    cuando faltan 47 h es adelantar el cierre.
    """
    if closes_at is None:
        return "—"
    if closes_at.tzinfo is None:
        closes_at = closes_at.replace(tzinfo=UTC)
    remaining = closes_at - now
    if remaining.total_seconds() <= 0:
        return "cerrado"
    if remaining < timedelta(hours=1):
        return f"en {int(remaining.total_seconds() // 60)} min"
    if remaining < timedelta(days=2):
        return f"en {remaining.days * 24 + remaining.seconds // 3600} h"
    return f"en {remaining.days} días"


class PredictionPage(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar en la pregunta… (vacío = todos)")
        self._search.returnPressed.connect(self._on_search)
        top.addWidget(self._search, stretch=1)
        top.addWidget(QLabel("Cierran en:"))
        self._window = QComboBox()
        for etiqueta, ventana in _WINDOWS:
            self._window.addItem(etiqueta, ventana)
        self._window.setToolTip(
            "Limita la lista a los mercados que cierran dentro de esa ventana y la "
            "ordena por el reloj. «todas» es la vista por volumen."
        )
        self._window.currentIndexChanged.connect(self._on_window_changed)
        top.addWidget(self._window)
        self._btn = QPushButton("Buscar")
        self._btn.clicked.connect(self._on_search)
        top.addWidget(self._btn)
        lay.addLayout(top)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Pregunta", "Favorito", "Total %", "Overround", "Coherente", "Cierra"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(True)
        lay.addWidget(self._table, stretch=2)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._detail)

        # --- Cestas con margen -------------------------------------------------
        basket_bar = QHBoxLayout()
        basket_bar.addWidget(QLabel("<b>Cestas con margen</b> (comprar todo cuesta menos de lo que paga)"))
        basket_bar.addStretch()
        basket_bar.addWidget(QLabel("Umbral:"))
        self._min_edge = QSpinBox()
        self._min_edge.setRange(0, 5_000)
        self._min_edge.setSingleStep(10)
        self._min_edge.setValue(DEFAULT_MIN_EDGE_BPS.value)
        self._min_edge.setSuffix(" bps")
        self._min_edge.setToolTip(
            "Margen mínimo para mostrarla. Por debajo de 50 bps el ruido de redondeo "
            "de la fuente domina: el tick mínimo de Polymarket ya son 100 bps."
        )
        self._min_edge.valueChanged.connect(self._refresh_baskets)
        basket_bar.addWidget(self._min_edge)
        self._edge_hint = QLabel("")
        self._edge_hint.setStyleSheet(f"color: {COLOR_MUTED};")
        basket_bar.addWidget(self._edge_hint)
        lay.addLayout(basket_bar)

        self._baskets = QTableWidget(0, 5)
        self._baskets.setHorizontalHeaderLabels(
            ["Pregunta", "Resultados", "Coste", "Descuento", "Retorno s/ capital"]
        )
        self._baskets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._baskets.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._baskets.setAlternatingRowColors(True)
        self._baskets.setEditTriggers(QTableWidget.NoEditTriggers)
        self._baskets.setWordWrap(True)
        lay.addWidget(self._baskets, stretch=1)

        self._basket_note = QLabel(_BASKET_CAVEAT)
        self._basket_note.setWordWrap(True)
        self._basket_note.setTextFormat(Qt.RichText)
        self._basket_note.setStyleSheet("color: #f5c518; font-size: 11px;")
        lay.addWidget(self._basket_note)

        self._table.itemSelectionChanged.connect(self._on_select)
        self._all_reports: tuple[MarketReport, ...] = ()
        self._reports: tuple[MarketReport, ...] = ()
        self._update_edge_hint()

    # ------------------------------------------------------------------ #
    # Búsqueda
    # ------------------------------------------------------------------ #
    def _window_choice(self) -> timedelta | None:
        return self._window.currentData()

    def _on_search(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando Polymarket…")
        spawn(self._do_search())

    async def _do_search(self) -> None:
        try:
            text = self._search.text().strip() or None
            window = self._window_choice()
            reports = await self._container.analyze_markets(
                limit=30, search=text, closing_within=window
            )
            self._all_reports = reports
            self._show(reports)
            self._status.setText(
                f"{len(self._reports)} mercado(s)"
                + (" · lo que antes cierra, primero" if window else " · lo incoherente primero")
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._table.setRowCount(0)
            self._baskets.setRowCount(0)
            self._all_reports = ()
            self._reports = ()
        finally:
            self._btn.setEnabled(True)

    def _on_window_changed(self) -> None:
        """Recorta lo ya traído, sin volver a pedir nada a la fuente.

        Sólo **recorta**: cambiar a una ventana más corta nunca puede añadir
        filas, porque lo que no se trajo no está. Con «todas» se recupera la
        lista entera tal como llegó, que es lo que hace que el selector sea
        reversible y se pueda probar sin coste.
        """
        window = self._window_choice()
        if window is None:
            self._show(self._all_reports)
            return
        now = self._container.clock.now()
        self._show(
            tuple(
                report
                for report in self._all_reports
                if (left := report.market.time_left(now)) is not None and left <= window
            )
        )

    def _show(self, reports: tuple[MarketReport, ...]) -> None:
        """Pinta los informes que quedan visibles y recalcula las cestas.

        `self._reports` es lo que se está viendo, no lo que se trajo: la
        selección de una fila y las cestas se resuelven contra esa lista, y
        dejarla apuntando a los informes sin filtrar haría que elegir la tercera
        fila hablara del tercer mercado de otra lista.
        """
        self._reports = reports
        self._fill(reports)
        self._refresh_baskets()

    def _fill(self, reports: tuple[MarketReport, ...]) -> None:
        now = self._container.clock.now()
        self._table.setRowCount(0)
        for rep in reports:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(rep.question))
            self._table.setItem(row, 1, QTableWidgetItem(f"{rep.favourite.label} ({rep.favourite.implied_percent:.1f} %)"))
            self._table.setItem(row, 2, QTableWidgetItem(f"{rep.total_percent:.2f} %"))
            self._table.setItem(row, 3, QTableWidgetItem(str(rep.overround_bps)))
            self._table.setItem(row, 4, QTableWidgetItem("✓" if rep.is_coherent else "✗"))
            closes_at = rep.market.closes_at
            item = QTableWidgetItem(countdown(closes_at, now))
            # La fecha exacta sigue disponible: la cuenta atrás es para leer la
            # tabla de un vistazo, no para esconder el dato.
            item.setToolTip(
                closes_at.isoformat() if closes_at is not None else "La fuente no publica fecha de cierre"
            )
            self._table.setItem(row, 5, item)
        self._table.resizeRowsToContents()

    def _on_select(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or not self._reports:
            return
        rep = self._reports[rows[0].row()]
        outcomes = "  |  ".join(f"{o.label}: {o.implied_percent:.1f} %" for o in rep.market.outcomes)
        self._detail.setText(f"{rep.note}  —  {outcomes}")

    # ------------------------------------------------------------------ #
    # Cestas
    # ------------------------------------------------------------------ #
    def _update_edge_hint(self) -> None:
        percent = BasisPoints(self._min_edge.value()).as_percent()
        self._edge_hint.setText(f"= {percent:f} %")

    def _refresh_baskets(self) -> None:
        """Recalcula las cestas sobre los informes ya traídos. Sin red."""
        self._update_edge_hint()
        self._baskets.setRowCount(0)
        if not self._reports:
            return
        found = self._container.find_prediction_opportunities.from_reports(
            self._reports,
            min_edge_bps=BasisPoints(self._min_edge.value()),
        )
        for opportunity in found:
            self._add_basket_row(opportunity)
        self._baskets.resizeRowsToContents()

    def _add_basket_row(self, opportunity: BasketOpportunity) -> None:
        row = self._baskets.rowCount()
        self._baskets.insertRow(row)
        self._baskets.setItem(row, 0, QTableWidgetItem(opportunity.question))
        self._baskets.setItem(row, 1, QTableWidgetItem(str(opportunity.outcome_count)))

        cost = QTableWidgetItem(f"{opportunity.cost:.4f}")
        # El coste por debajo de 1 es *la* señal; se marca en verde para que la
        # fila se lea de un vistazo sin tener que interpretar la columna.
        cost.setForeground(QColor(COLOR_SUCCESS))
        self._baskets.setItem(row, 2, cost)

        self._baskets.setItem(row, 3, QTableWidgetItem(str(opportunity.discount_bps)))
        self._baskets.setItem(row, 4, QTableWidgetItem(str(opportunity.return_on_cost_bps)))

        # El desglose completo y la nota van en el tooltip: la fila tiene que
        # seguir siendo legible de un vistazo.
        breakdown = "  |  ".join(
            f"{o.label}: {o.price}" for o in opportunity.outcomes
        )
        for column in range(self._baskets.columnCount()):
            item = self._baskets.item(row, column)
            if item is not None:
                item.setToolTip(f"{breakdown}\n\n{opportunity.note}")
