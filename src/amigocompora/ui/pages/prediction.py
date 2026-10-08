"""Pestaña: Mercados de predicción — mirarlos **y operarlos**.

Cuatro zonas. Arriba la búsqueda; en medio, a la izquierda la tabla de mercados y a
la derecha la tarjeta de orden; abajo las cestas con margen y, debajo, lo que ya
se resolvió y se puede cobrar.

La tarjeta de orden es la razón de que esta pestaña tenga la forma que tiene. La
versión anterior leía mercados y no ofrecía ninguna forma de comprar ni de
vender: era una pestaña que describía un mercado en el que no se podía
participar, y el botón de operar que el usuario buscaba no existía en ninguna
parte. Ahora, al seleccionar un mercado, la tarjeta se llena con **ese** mercado
—sus resultados, su salto de precio, su mínimo de participaciones, el libro real
del resultado elegido— y el botón de firmar aparece apagado con el motivo escrito
cuando falta algo, que es lo mismo que hace la pestaña de swap.

### Dos hechos que mandan en el diseño

1. **El libro es un hecho y el precio publicado es una opinión.** La probabilidad
   implícita de un resultado es el último precio cruzado; lo que costaría cruzar
   ahora un tamaño dado es otra cosa. La tarjeta pide el libro del resultado
   elegido y enseña las dos cifras, porque la diferencia entre ellas es
   exactamente el margen de la operación.
2. **Publicar no es emitir.** Una orden firmada se puede cancelar mientras nadie
   la haya cruzado; una transacción emitida no. Todo lo que dice la tarjeta está
   redactado para que esa diferencia no se pierda.

### Y una tercera zona, que va en la dirección contraria

La tarjeta de cobro cierra el ciclo de las dos anteriores: lo que se compró y
salió a favor hay que **cobrarlo**, y hasta que no se cobra el colateral sigue
dentro del contrato. Es la única operación de la aplicación por la que el dinero
**entra**, y de ahí sale lo que la distingue: no le aplican los topes de gasto
—acotan lo que sale— y no pide ningún permiso previo, porque el contrato quema
las participaciones de quien firma. Lo que sí comparte con las otras dos es la
regla del botón: apagado **con el motivo escrito**.

### Sobre los dos umbrales

Los dos umbrales de las cestas son controles de la vista, no de la consulta: ni
el de margen ni el de ventana vuelven a pedir nada a la fuente. El de margen
recalcula las cestas sobre los informes ya traídos
(`FindPredictionOpportunities.from_reports`) y el de ventana recorta la lista de
mercados ya traída. Es la razón por la que esos métodos están separados, y aquí
es donde se aprovechan.

La ventana, además, **se pide** en la siguiente búsqueda: con «24 h» el motor
ordena por fecha de cierre y filtra en el servidor, para que los 30 mercados que
se traen sean los que cierran pronto y no los 30 de más volumen de los cuales sólo
tres cierran mañana. Recortar en cliente lo ya traído no basta para eso, y por eso
se hacen las dos cosas.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
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
from amigocompora.domain.models import (
    MarketDepth,
    MarketOutcome,
    PredictionMarket,
    PredictionPosition,
    PredictionSide,
    Token,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints
from amigocompora.ui.theme import (
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_SUCCESS,
)
from amigocompora.ui.widgets import Card, Chip, Field, divider, set_empty, spawn

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

#: Alto de la tabla de cobro y de su rótulo. Los dos lo comparten para que el
#: bloque no cambie de tamaño al pasar de uno a otro —una tabla vacía no es
#: pequeña, y sin esto el salto se ve como un parpadeo del diseño—.
_REDEEM_TABLE_HEIGHT = 150


def _motivos(motivos: Sequence[str]) -> str:
    """Une los motivos de un bloqueo en una frase, con **un** punto al final.

    Algunos motivos llegan ya redactados por quien los lanzó —el registro de
    motores, por ejemplo— y acaban en punto. Pegarles otro detrás deja
    «...panel de motores..» en la pantalla, y ese detalle es el que hace dudar de
    que haya alguien leyendo lo que la aplicación escribe. Se recorta el punto de
    cada uno y se pone uno solo, al final de la frase entera.
    """
    return " · ".join(motivo.rstrip(".") for motivo in motivos) + "."


def _decimals_of(tick: Decimal) -> int:
    """Cuántos decimales caben en un salto de precio.

    El salto **es** la precisión del mercado: con un salto de 0.01 no existe el
    precio 0.555, y un campo que lo admitiera dejaría escribir un número que el
    recinto rechazaría al firmarlo. Se deriva del propio salto en vez de fijarlo
    a un número redondo, porque el salto lo publica la fuente y cambia de un
    mercado a otro.
    """
    exponent = tick.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Baja un precio al múltiplo de `tick` inmediatamente inferior.

    Hacia **abajo** y no al más cercano, y esa dirección es deliberada: el
    precio que se propone sale de un precio ya cruzado, y redondearlo hacia
    arriba propondría pagar más de lo que el mercado está pidiendo. En una orden
    de compra el error se paga; en una de venta se deja de cobrar. La bajada es
    la dirección en la que el número propuesto nunca es peor que el observado.
    """
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


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
        #: La página entera, guardada porque el alto de las cestas se ajusta en
        #: tiempo de ejecución: ver `_refresh_baskets`.
        self._root_layout = lay

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

        #: El rótulo que ocupa el sitio de la tabla mientras está vacía. Sin él,
        #: al abrir la pestaña queda un rectángulo gris con encabezados y nada
        #: dentro: no se distingue «todavía no has buscado» de «no hay nada».
        self._markets_empty = QLabel("")
        self._markets_empty.setObjectName("empty")
        self._markets_empty.setWordWrap(True)
        self._markets_empty.setAlignment(Qt.AlignCenter)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(f"color: {COLOR_MUTED};")

        # La tabla y la tarjeta de orden, lado a lado. En columna, la tarjeta
        # empujaría las cestas fuera de la pantalla —son cuatro bloques
        # apilados— y obligaría a desplazarse para ver lo que se está operando a
        # la vez que el mercado que se está mirando. En paralelo, elegir una fila
        # y ver lo que se puede hacer con ella ocurre en el mismo sitio.
        middle = QHBoxLayout()
        middle.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._table, stretch=1)
        left.addWidget(self._markets_empty, stretch=1)
        left.addWidget(self._detail)
        middle.addLayout(left, stretch=1)
        middle.addWidget(self._build_order_card())
        lay.addLayout(middle, stretch=2)

        self._table.itemSelectionChanged.connect(self._on_select)

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

        #: Mismo recurso que en la pestaña de precios: cuando no hay cestas, el
        #: sitio de la tabla lo ocupa la explicación. Sin esto queda un
        #: rectángulo gris del alto entero que no distingue «todavía no has
        #: buscado» de «buscaste y ninguna cesta llega al umbral», que son dos
        #: cosas distintas y se arreglan de forma distinta.
        self._baskets_empty = QLabel("")
        self._baskets_empty.setObjectName("empty")
        self._baskets_empty.setWordWrap(True)
        self._baskets_empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._baskets_empty, stretch=1)

        self._basket_note = QLabel(_BASKET_CAVEAT)
        self._basket_note.setWordWrap(True)
        self._basket_note.setTextFormat(Qt.RichText)
        self._basket_note.setStyleSheet("color: #f5c518; font-size: 11px;")
        lay.addWidget(self._basket_note)

        # --- Cobrar lo que ya se resolvió --------------------------------------
        lay.addWidget(self._build_redeem_card())

        self._all_reports: tuple[MarketReport, ...] = ()
        self._reports: tuple[MarketReport, ...] = ()
        #: Si ya se ha pedido alguna vez. Distingue «todavía no has buscado» de
        #: «buscaste y no hay», que son dos textos distintos y dos acciones
        #: distintas para el usuario.
        self._searched = False
        #: El motivo del último fallo, o `None`. Lo que se enseña en la tabla
        #: vacía depende de esto: no es lo mismo «no hay mercados» que «la
        #: consulta no llegó a hacerse».
        self._last_error: str | None = None
        #: El libro del resultado elegido, o `None` mientras no se haya leído.
        #: Vive aquí y no se pide en cada repintado: es una lectura de red, y
        #: repintar un campo no es motivo para salir a la red.
        self._depth: MarketDepth | None = None
        #: Si el precio del campo sigue siendo la propuesta del panel o lo
        #: escribió el usuario. Una propuesta se refresca cuando llega el libro;
        #: un precio escrito a mano no se toca nunca.
        self._price_auto = True
        #: Lo que la cartera que firma tiene resuelto y sin cobrar. Vive aquí y no
        #: se pide al pintar: es una lectura de red, y repintar no es motivo para
        #: salir a la red. `_positions_read` distingue «todavía no se ha leído» de
        #: «se leyó y no hay nada», que son dos textos y dos acciones distintas.
        self._positions: tuple[PredictionPosition, ...] = ()
        self._positions_read = False
        self._positions_error: str | None = None
        self._update_edge_hint()
        # Y las cestas se pintan ya, aunque no haya nada que pintar: al abrir la
        # pestaña, sin esto, quedaba un rectángulo gris del alto entero sin una
        # palabra que dijera que hace falta buscar. La explicación se pone en el
        # momento de construir la pantalla, no en el de usarla.
        self._refresh_baskets()
        self._refresh_order_state()
        # Y la tarjeta de cobro, con su tabla y su rótulo, desde el primer
        # momento: al abrir la pestaña el botón tiene que estar apagado **por algo
        # escrito**, no por no haberse pintado todavía.
        self._fill_positions()
        self._refresh_redeem_state()
        # Y la tabla de mercados, con su rótulo, desde el primer momento.
        self._fill(())

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
            self._searched = True
            self._last_error = None
            self._show(reports)
            self._status.setText(
                f"{len(self._reports)} mercado(s)"
                + (" · lo que antes cierra, primero" if window else " · lo incoherente primero")
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._all_reports = ()
            self._searched = True
            #: El motivo se guarda para que la tabla pueda decir que no está
            #: vacía por no haber nada, sino porque la consulta no llegó a
            #: hacerse. Son dos cosas distintas y el usuario las arregla distinto.
            self._last_error = str(error)
            self._clear_order_card()
            self._show(())
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

    def _clear_order_card(self) -> None:
        """Deja la tarjeta de orden sin mercado.

        Hace falta porque la tarjeta **sobrevive** a la tabla: cuando una
        búsqueda reemplaza las filas, la selección desaparece y la tarjeta se
        quedaría enseñando el mercado anterior con sus precios y su libro como si
        siguiera elegido. Operar sobre lo que la tabla ya no muestra es la forma
        más silenciosa de firmar lo que no se está mirando.
        """
        self._depth = None
        self._price_auto = True
        self._outcome.blockSignals(True)
        self._outcome.clear()
        self._outcome.blockSignals(False)
        self._order_market.setText("Selecciona un mercado de la tabla.")
        self._order_cost.setText("—")
        self._order_payout.setText("")
        self._book_label.setText("")
        self._order_status.setText("")
        self._refresh_order_state()

    def _fill(self, reports: tuple[MarketReport, ...]) -> None:
        now = self._container.clock.now()
        self._table.setRowCount(0)
        self._clear_order_card()
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
        # Igual que en las cestas: al abrir la pestaña la tabla de mercados era un
        # rectángulo gris del alto entero con seis encabezados encima y nada
        # dentro, que no se distingue de una búsqueda sin resultados.
        set_empty(self._table, self._markets_empty, self._why_no_rows())

    def _why_no_rows(self) -> str:
        """Por qué la tabla de mercados está vacía, en el caso que sea.

        Son cuatro situaciones distintas y cada una se arregla de una forma: no
        haber buscado (pulsa Buscar), que la búsqueda fallara (mira arriba), que
        la fuente no tenga nada para esa palabra (cámbiala) y que el filtro de
        cierre las haya quitado todas (ábrelo). Un solo texto para todas
        obligaría a adivinar cuál es.
        """
        if self._last_error is not None:
            return (
                "La búsqueda no llegó a completarse: el motivo está en la línea "
                "de arriba. No se ha firmado ni publicado nada."
            )
        if not self._searched:
            return (
                "Pulsa «Buscar» para traer los mercados abiertos de Polymarket. "
                "Leerlos es una consulta pública: no firma ni publica nada."
            )
        if not self._all_reports:
            return (
                "La fuente no devolvió ningún mercado abierto para esa búsqueda. "
                "Prueba con otra palabra, o vacía el campo y vuelve a buscar."
            )
        return (
            f"Ninguno de los {len(self._all_reports)} mercados traídos cierra "
            "dentro de esa ventana. Ponla en «todas» para verlos todos."
        )

    def _on_select(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or not self._reports:
            return
        rep = self._reports[rows[0].row()]
        outcomes = "  |  ".join(f"{o.label}: {o.implied_percent:.1f} %" for o in rep.market.outcomes)
        self._detail.setText(f"{rep.note}  —  {outcomes}")
        self._fill_order_card(rep.market)

    def _fill_order_card(self, market: PredictionMarket) -> None:
        """Carga el mercado elegido en la tarjeta de orden, y pide su libro.

        Se reconstruye entera y no se parchea campo a campo: los límites del
        campo de precio —el salto, los decimales, el rango— son **del mercado**,
        y dejar los del anterior mientras se cambia de fila deja escribir durante
        un instante una orden que el recinto rechazaría.
        """
        self._order_market.setText(f"<b>{market.question}</b>")
        self._order_market.setTextFormat(Qt.RichText)
        self._order_status.setText("")
        self._depth = None
        # Mercado nuevo, propuesta nueva: el precio que hubiera escrito para el
        # mercado anterior no dice nada de éste.
        self._price_auto = True

        # Se bloquean las señales mientras se rellena: `addItem` dispara
        # `currentIndexChanged` en la primera entrada, y eso lanzaría una lectura
        # de libro por cada mercado que se va poblando.
        self._outcome.blockSignals(True)
        self._outcome.clear()
        for outcome in market.outcomes:
            self._outcome.addItem(
                f"{outcome.label} — {outcome.implied_percent:.1f} %", outcome.label
            )
        self._outcome.blockSignals(False)

        self._apply_market_limits()
        self._refresh_order_state()
        spawn(self._do_load_book())

    # ------------------------------------------------------------------ #
    # Operar: la tarjeta de orden
    # ------------------------------------------------------------------ #
    def _build_order_card(self) -> Card:
        """La tarjeta que convierte un mercado leído en una orden firmable."""
        card = Card("Operar", subtitle="— orden límite")
        card.setMinimumWidth(380)
        card.setMaximumWidth(460)

        self._order_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._order_chip)

        self._order_market = QLabel("Selecciona un mercado de la tabla.")
        self._order_market.setObjectName("hint")
        self._order_market.setWordWrap(True)
        card.body().addWidget(self._order_market)

        # Resultado y lado en la misma fila: son las dos mitades de «qué» se
        # opera, y comprar «No» es una operación distinta de comprar «Sí».
        self._outcome = QComboBox()
        self._outcome.setToolTip(
            "Resultado del mercado. Se puede comprar o vender cualquiera de "
            "ellos: vender «Sí» y comprar «No» no son la misma operación."
        )
        self._outcome.currentIndexChanged.connect(self._on_outcome_changed)
        campo_resultado = Field("RESULTADO")
        campo_resultado.add(self._outcome, 1)

        self._side = QComboBox()
        self._side.addItem("Comprar", PredictionSide.BUY)
        self._side.addItem("Vender", PredictionSide.SELL)
        self._side.currentIndexChanged.connect(self._on_side_changed)
        campo_lado = Field("LADO")
        campo_lado.add(self._side)

        fila = QHBoxLayout()
        fila.setSpacing(8)
        fila.addWidget(campo_resultado, 2)
        fila.addWidget(campo_lado, 1)
        card.add_row(fila)

        self._shares = QDoubleSpinBox()
        self._shares.setDecimals(2)
        self._shares.setRange(1.0, 1_000_000.0)
        self._shares.setToolTip(
            "Participaciones. Cada una paga 1 del colateral si el resultado "
            "ocurre, y 0 si no."
        )
        self._shares.valueChanged.connect(self._on_order_edited)
        campo_participaciones = Field("PARTICIPACIONES")
        campo_participaciones.add(self._shares, 1)

        self._price = QDoubleSpinBox()
        self._price.setDecimals(2)
        self._price.setRange(0.01, 0.99)
        self._price.setToolTip(
            "Precio máximo al comprar (mínimo al vender) por participación. Se "
            "propone desde el libro real y se ajusta al salto del mercado. La "
            "orden queda en el libro hasta que la canceles: no se ejecuta sola "
            "ni caduca."
        )
        self._price.valueChanged.connect(self._on_price_edited)
        campo_precio = Field("PRECIO LÍMITE")
        campo_precio.add(self._price, 1)

        fila2 = QHBoxLayout()
        fila2.setSpacing(8)
        fila2.addWidget(campo_participaciones, 1)
        fila2.addWidget(campo_precio, 1)
        card.add_row(fila2)

        card.body().addWidget(divider())

        # Lo que cuesta y lo que paga, en la misma línea y con las dos cifras
        # juntas: el coste sin el pago potencial es la mitad de la información
        # que hace falta para decidir.
        self._order_cost = QLabel("—")
        self._order_cost.setObjectName("bigNumber")
        card.body().addWidget(self._order_cost)
        self._order_payout = QLabel("")
        self._order_payout.setObjectName("hint")
        self._order_payout.setWordWrap(True)
        card.body().addWidget(self._order_payout)

        self._book_label = QLabel("")
        self._book_label.setObjectName("hint")
        self._book_label.setWordWrap(True)
        card.body().addWidget(self._book_label)

        self._book_btn = QPushButton("Leer el libro")
        self._book_btn.setObjectName("secondary")
        self._book_btn.setToolTip(
            "Vuelve a pedir el libro del resultado elegido. Es una lectura "
            "pública: no firma ni publica nada."
        )
        self._book_btn.clicked.connect(lambda: spawn(self._do_load_book()))
        card.body().addWidget(self._book_btn)

        self._submit_btn = QPushButton("Firmar y publicar la orden…")
        self._submit_btn.setObjectName("danger")
        self._submit_btn.setToolTip(
            "Firma la orden y la publica en el libro del recinto. No es una "
            "transacción y no cuesta gas, pero queda a la vista de todos y "
            "cualquiera puede cruzarla: revisa el precio antes."
        )
        self._submit_btn.clicked.connect(self._on_submit)
        card.body().addWidget(self._submit_btn)

        # Por qué está apagado, escrito y no escondido en el tooltip: un botón
        # apagado sin motivo se lee como «esto no funciona» y empuja a buscar la
        # forma de saltárselo.
        self._order_note = QLabel("")
        self._order_note.setWordWrap(True)
        self._order_note.setStyleSheet(f"color: {COLOR_DANGER};")
        card.body().addWidget(self._order_note)

        self._order_status = QLabel("")
        self._order_status.setObjectName("hint")
        self._order_status.setWordWrap(True)
        card.body().addWidget(self._order_status)
        card.body().addStretch()
        return card

    # ------------------------------------------------------------------ #
    # Cobrar: la tarjeta de lo que ya se resolvió
    # ------------------------------------------------------------------ #
    def _build_redeem_card(self) -> Card:
        """La tarjeta que convierte una posición resuelta en una transacción.

        Cobrar no se parece a operar: no hay precio que negociar, ni libro, ni
        contraparte, ni salto que respetar. Por eso vive en su propia tarjeta y no
        dentro de la de orden, donde todo gira alrededor de un precio que un cobro
        no tiene.

        Lo que sí comparte con ella es lo importante: el botón sale apagado **con
        el motivo escrito** cuando falta algo, y la regla de cuándo se enseña ese
        motivo es la misma —sólo cuando ya hay algo que cobrar—, porque un aviso
        permanente en rojo se aprende a ignorar.

        Y tiene una frase que la otra no necesita: **esto entra dinero**. Quien
        venga de operar espera lo contrario, y creer que se está pagando lo que en
        realidad se está cobrando es la clase de duda que se resuelve escribiéndola.
        """
        card = Card("Por cobrar", subtitle="— una transacción por mercado")

        self._redeem_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._redeem_chip)

        intro = QLabel(
            "Lo que salió a favor sigue dentro del contrato hasta que se cobra: el "
            "mercado resuelve, pero el colateral no vuelve solo. Se lee de la "
            "cartera que firma y se cobra por mercado, porque el contrato quema en "
            "una sola llamada las participaciones de los dos resultados. "
            "<b>No consume el tope de gasto</b>: los topes acotan lo que sale, y "
            "esto entra."
        )
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        card.body().addWidget(intro)

        self._redeem_table = QTableWidget(0, 5)
        self._redeem_table.setHorizontalHeaderLabels(
            ["Mercado", "Resultado", "Participaciones", "Cobras", "Estado"]
        )
        self._redeem_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self._redeem_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._redeem_table.setAlternatingRowColors(True)
        self._redeem_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._redeem_table.setWordWrap(True)
        self._redeem_table.setMinimumHeight(_REDEEM_TABLE_HEIGHT)

        #: El rótulo que ocupa el sitio de la tabla mientras está vacía, con la
        #: misma altura que ella para que el alto no salte al cambiar de uno a
        #: otro. Es el mismo recurso que en las cestas, y distingue «todavía no has
        #: leído» de «leíste y no hay nada», que se arreglan de forma distinta.
        self._redeem_empty = QLabel("")
        self._redeem_empty.setObjectName("empty")
        self._redeem_empty.setWordWrap(True)
        self._redeem_empty.setAlignment(Qt.AlignCenter)
        self._redeem_empty.setMinimumHeight(_REDEEM_TABLE_HEIGHT)
        card.body().addWidget(self._redeem_table)
        card.body().addWidget(self._redeem_empty)

        fila = QHBoxLayout()
        fila.setSpacing(8)
        self._redeem_read_btn = QPushButton("Buscar lo que puedo cobrar")
        self._redeem_read_btn.setObjectName("secondary")
        self._redeem_read_btn.setToolTip(
            "Lee las posiciones resueltas de la cartera que firma. Es una lectura "
            "pública: no firma ni emite nada."
        )
        self._redeem_read_btn.clicked.connect(lambda: spawn(self._do_read_positions()))
        fila.addWidget(self._redeem_read_btn, 1)

        self._redeem_btn = QPushButton("Cobrar…")
        self._redeem_btn.setObjectName("danger")
        self._redeem_btn.setToolTip(
            "Emite una transacción por mercado y no se puede deshacer. Cuesta gas "
            "y lo que se cobra entra en la cartera que firma."
        )
        self._redeem_btn.clicked.connect(self._on_redeem)
        fila.addWidget(self._redeem_btn, 1)
        card.add_row(fila)

        self._redeem_note = QLabel("")
        self._redeem_note.setWordWrap(True)
        self._redeem_note.setStyleSheet(f"color: {COLOR_DANGER};")
        card.body().addWidget(self._redeem_note)

        self._redeem_status = QLabel("")
        self._redeem_status.setObjectName("hint")
        self._redeem_status.setWordWrap(True)
        card.body().addWidget(self._redeem_status)
        return card

    def _redeemable(self) -> tuple[PredictionPosition, ...]:
        """Lo que de verdad se puede cobrar, y no todo lo que se ha leído.

        La fuente dice si el mercado resolvió, pero no si comparte colateral con
        otros: eso lo decide el dominio, y una posición que la fuente da por
        cobrable puede estar bloqueada aquí. Filtrar por `is_redeemable` es lo que
        hace que el número del botón sea el número de transacciones que se van a
        firmar y no el de filas de la tabla.
        """
        return tuple(posicion for posicion in self._positions if posicion.is_redeemable)

    def _mercados_cobrables(self) -> int:
        """Cuántos **mercados** hay que cobrar, que es cuántas transacciones son."""
        return len({posicion.condition_id for posicion in self._redeemable()})

    async def _do_read_positions(self) -> None:
        """Lee las posiciones de la cartera que firma. Lectura pública, sin claves.

        Se piden **sólo las cobrables** al servidor. Una cartera con historial
        tiene cientos de posiciones cerradas o perdedoras, y traérselas todas para
        descartarlas aquí sería traer la mayor parte de la respuesta para tirarla.
        """
        wallet = self._container.keys.address()
        if wallet is None:
            self._positions = ()
            self._positions_read = True
            self._positions_error = None
            self._redeem_status.setText(
                "No hay cartera configurada, así que no hay posiciones que leer."
            )
            self._fill_positions()
            self._refresh_redeem_state()
            return

        self._redeem_read_btn.setEnabled(False)
        self._redeem_status.setText("Leyendo lo que hay para cobrar…")
        try:
            redeemer = self._container.registry.prediction_redeemer()
            positions = await redeemer.positions(wallet=wallet, redeemable_only=True)
        except Exception as error:
            # Un fallo de lectura no se disfraza de «no hay nada»: son dos cosas
            # distintas y llevan a acciones distintas —mirar el motor, o mirar el
            # mercado—. Por eso el error se guarda y lo dice el rótulo vacío.
            self._positions = ()
            self._positions_error = str(error)
        else:
            self._positions = tuple(positions)
            self._positions_error = None
            self._redeem_status.setText("")
        finally:
            self._positions_read = True
            self._redeem_read_btn.setEnabled(True)
        self._fill_positions()
        self._refresh_redeem_state()

    def _fill_positions(self) -> None:
        """Pinta la tabla de lo que hay por cobrar, y su rótulo si no hay nada."""
        self._redeem_table.setRowCount(len(self._positions))
        for fila, posicion in enumerate(self._positions):
            bloqueos = posicion.redeemability_blockers
            celdas = (
                posicion.question,
                posicion.outcome_label,
                f"{posicion.shares:f}",
                f"{posicion.payout:f}",
                "; ".join(bloqueos) if bloqueos else "se puede cobrar",
            )
            for columna, texto in enumerate(celdas):
                celda = QTableWidgetItem(texto)
                if columna == 4:
                    # El estado se lee antes que las cifras, así que lleva el
                    # color: lo que no se puede cobrar se apaga en vez de
                    # desaparecer —sigue siendo dinero del usuario, sólo que
                    # todavía no—.
                    celda.setForeground(QColor(COLOR_MUTED if bloqueos else COLOR_SUCCESS))
                self._redeem_table.setItem(fila, columna, celda)
        set_empty(self._redeem_table, self._redeem_empty, self._redeem_empty_text())

    def _redeem_empty_text(self) -> str:
        """Por qué la tabla está vacía. Nunca es lo mismo, y nunca es «error»."""
        if self._positions_error is not None:
            return (
                f"No se pudieron leer las posiciones: {self._positions_error}"
            )
        if not self._positions_read:
            return (
                "Pulsa «Buscar lo que puedo cobrar» para leer lo que esta cartera "
                "tiene resuelto y sin cobrar."
            )
        return (
            "No hay nada resuelto a tu favor en esta cartera. Una posición aparece "
            "aquí cuando el mercado ya resolvió; mientras tanto sigue contando "
            "como abierta, y el colateral no vuelve solo."
        )

    def _redeem_blockers(self) -> tuple[str, ...]:
        """Todo lo que falta para poder cobrar. Vacío significa que sí.

        Mira lo mismo que mira `RedeemPrediction` antes de firmar, y en el mismo
        orden: el modo, el interruptor, la red, el colateral, el motor y la
        cartera. Si esta lista dijera que sí y el caso de uso dijera que no, la
        interfaz estaría prometiendo algo que no cumple.

        **El importe no aparece aquí, y es lo único que no aparece**: un cobro no
        compromete dinero, así que los topes de gasto no le aplican. Ponerlo
        dejaría el botón apagado por una cantidad que no se gasta, y en el peor
        momento —cuanto más ganada está una posición, más grande es su cobro—.
        """
        container = self._container
        motivos: list[str] = []
        if not container.guard.mode.grants(Capability.BROADCAST_TX):
            motivos.append(
                f"el modo {container.guard.mode.label} no permite emitir "
                "(cambia a EJECUCIÓN)"
            )
        if not container.policy.limits.enabled:
            motivos.append(
                "la ejecución está apagada (`enabled = true` bajo `[execution]` "
                "en config.toml)"
            )
        if not container.keys.available():
            motivos.append("no hay cartera configurada, así que no hay quién firme")
            # Sin cartera no hay red que comprobar: las posiciones se leen de una
            # cartera, y sin ella no hay ni una que mirar. Se corta aquí en vez de
            # seguir preguntando por una cadena que todavía no se conoce.
            return tuple(motivos)

        redeemer = None
        try:
            redeemer = container.registry.prediction_redeemer()
        except Exception as error:
            # El registro ya explica esto con nombre propio —el motor activo lee
            # mercados pero no sabe cobrar—, y aquí se repite tal cual en vez de
            # resumirlo: el texto del registro dice qué hacer.
            motivos.append(str(error))
        if redeemer is None:
            return tuple(motivos)

        limites = container.policy.limits
        if self._positions:
            cadena = self._positions[0].venue.chain
            if cadena not in limites.allowed_chains:
                motivos.append(
                    f"la red «{cadena}» no está en `allowed_chains` "
                    f"(ahora declara: {', '.join(sorted(limites.allowed_chains)) or 'nada'})"
                )
            try:
                colateral = redeemer.collateral_on(cadena)
            except Exception as error:
                motivos.append(f"no se pudo determinar el colateral del recinto: {error}")
            else:
                if colateral.symbol not in limites.allowed_tokens:
                    # Se dice también **qué hay declarado**: el error más común no
                    # es olvidar la lista, es escribir en ella un nombre que ningún
                    # token tiene.
                    motivos.append(
                        f"el colateral «{colateral.symbol}» no está en "
                        f"`allowed_tokens` (ahora declara: "
                        f"{', '.join(sorted(limites.allowed_tokens)) or 'nada'}; "
                        f"añádelo en config.toml)"
                    )
        engine_id = redeemer.manifest.engine_id
        if engine_id not in limites.allowed_engines:
            motivos.append(
                f"el motor «{engine_id}» no está en `allowed_engines` "
                f"(ahora declara: {', '.join(sorted(limites.allowed_engines)) or 'nada'})"
            )
        return tuple(motivos)

    def _refresh_redeem_state(self) -> None:
        """Repinta el botón de cobrar, su motivo y su recuento."""
        mercados = self._mercados_cobrables()
        motivos = self._redeem_blockers()
        self._redeem_btn.setEnabled(mercados > 0 and not motivos)
        # El motivo se enseña sólo cuando ya hay algo que cobrar: antes de eso el
        # botón apagado no dice nada que el usuario no sepa —todavía no ha leído
        # nada—, y un aviso permanente en rojo se aprende a ignorar. Misma regla
        # que en la tarjeta de orden.
        self._redeem_note.setText(
            ""
            if mercados == 0 or not motivos
            else "No se puede cobrar: " + _motivos(motivos)
        )
        puede = self._container.guard.allows(Capability.BROADCAST_TX)
        self._redeem_chip.set_state(
            f"MODO {self._container.guard.mode.label}",
            COLOR_SUCCESS if puede else COLOR_MUTED,
        )
        # El recuento va en el botón porque es lo que se va a firmar: una
        # transacción por mercado, y no una por fila de la tabla.
        self._redeem_btn.setText(
            f"Cobrar {mercados} mercado{'s' if mercados != 1 else ''}…"
            if mercados
            else "Cobrar…"
        )

    def _on_redeem(self) -> None:
        """Pide cobrar lo que hay. El «sí» de cada mercado lo pide el caso de uso."""
        cobrables = self._redeemable()
        if not cobrables:
            self._redeem_status.setText("No hay ninguna posición cobrable cargada.")
            return
        motivos = self._redeem_blockers()
        if motivos:
            # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el
            # modo se puede cambiar desde la barra mientras esta pestaña está
            # abierta, y un botón sólo se repinta cuando algo lo repinta.
            self._redeem_status.setText("No se puede cobrar: " + _motivos(motivos))
            return
        wallet = self._container.keys.address()
        if wallet is None:
            self._redeem_status.setText(
                "No hay cartera configurada, así que no hay quién cobre."
            )
            return
        self._redeem_btn.setEnabled(False)
        self._redeem_status.setText(
            f"Cobrando {self._mercados_cobrables()} mercado(s)… cada uno se "
            "confirma aparte, porque cada uno es una transacción."
        )
        spawn(self._do_redeem(cobrables, wallet))

    async def _do_redeem(
        self, positions: tuple[PredictionPosition, ...], wallet: str
    ) -> None:
        """Cobra lo seleccionado, con la cartera que firma como destinataria.

        El destinatario es la propia cartera que firma y no se pregunta: el
        contrato devuelve el colateral a quien llama —`redeemPositions` no tiene
        parámetro de destinatario—, así que ofrecer otro sería ofrecer algo que el
        protocolo no tiene. Y tiene que ser la misma cartera de la que se leyeron
        las posiciones, porque el contrato quema las de quien firma.
        """
        try:
            recibos = await self._container.redeem_prediction(
                positions, wallet=wallet, recipient=wallet
            )
        except Exception as error:
            aviso = f"Error: {error}"
        else:
            detalle = ", ".join(
                f"{recibo.tx_hash[:10]}… ({recibo.status.value})" for recibo in recibos
            )
            aviso = (
                f"Cobrado · {len(recibos)} transacción(es): {detalle}. El colateral "
                "entró en la cartera que firma."
            )
        # Se vuelve a leer **antes** de escribir el resultado, y no después: lo
        # cobrado ya no está, y una tabla que siguiera enseñándolo sería una
        # invitación a cobrarlo otra vez. Escribir el aviso primero lo borraría,
        # porque la lectura deja su propio mensaje.
        await self._do_read_positions()
        if self._positions_error is not None:
            # La relectura falló, y callarlo dejaría el aviso diciendo que todo
            # está al día cuando la tabla puede seguir enseñando lo ya cobrado.
            aviso += f" (y no se pudo releer la lista: {self._positions_error})"
        self._redeem_status.setText(aviso)

    # ------------------------------------------------------------------ #
    # Estado de la orden
    # ------------------------------------------------------------------ #
    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo: los dos botones que firman.

        Público porque quien sabe que el modo ha cambiado es la ventana, no esta
        pestaña: `ModeGuard.subscribe` avisa a quien se apunte, y la pestaña no se
        apunta sola. Existe en vez de que la ventana llame a los privados porque un
        método privado de otra clase no es una interfaz, es un atajo.

        Y son **los dos**: el de publicar y el de cobrar. El modo decide si se
        puede emitir, así que sin esto los dos se quedan como estaban y el usuario
        descubre el cambio al pulsarlos —que es justo lo que enseña a desconfiar
        de los botones—.
        """
        self._refresh_order_state()
        self._refresh_redeem_state()

    def _market(self) -> PredictionMarket | None:
        """El mercado seleccionado, o `None` si no hay ninguno."""
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows or not self._reports:
            return None
        row = rows[0].row()
        return self._reports[row].market if 0 <= row < len(self._reports) else None

    def _outcome_label(self) -> str | None:
        label = self._outcome.currentData()
        return label if isinstance(label, str) else None

    def _selected_outcome(self) -> MarketOutcome | None:
        market = self._market()
        label = self._outcome_label()
        return None if market is None or label is None else market.outcome(label)

    def _on_outcome_changed(self) -> None:
        """Cambiar de resultado cambia de libro: es otro token, otra profundidad."""
        self._apply_market_limits()
        spawn(self._do_load_book())

    def _on_side_changed(self) -> None:
        """Cambiar de lado **vuelve a proponer el precio**, y eso es deliberado.

        El precio propuesto sale del libro del lado en el que se está: al comprar
        el mejor `ask`, al vender el mejor `bid`. Conservar el de la compra en una
        venta dejaría una orden que no se cruza —que en una orden límite significa
        quedarse mirando—, y el campo no es una decisión del usuario todavía: es
        una propuesta que él puede cambiar después. Lo que no puede es quedarse
        con una propuesta que ya no corresponde a lo que va a firmar.
        """
        self._price_auto = True
        self._apply_market_limits()
        self._refresh_order_state()

    def _on_price_edited(self) -> None:
        """El usuario escribió un precio: deja de ser una propuesta y es suyo.

        Se marca aquí y no comparando el valor con el propuesto porque pueden
        coincidir —bajar el precio al del libro es exactamente lo que alguien
        haría—, y en ese caso la intención del usuario tiene que ganar.
        """
        self._price_auto = False
        self._refresh_order_state()

    def _on_order_edited(self) -> None:
        self._refresh_order_state()

    def _apply_market_limits(self) -> None:
        """Ajusta los campos a las reglas **de este mercado**.

        El mínimo de participaciones y el salto de precio los publica la fuente
        y cambian de un mercado a otro; un campo con los límites de otro
        mercado deja escribir una orden que el recinto rechaza entera, y
        descubrirlo al firmar es descubrirlo después de haberla confirmado.
        """
        market = self._market()
        if market is None:
            return
        minimo = market.min_order_size or Decimal(1)
        tick = market.tick_size or Decimal("0.01")
        self._shares.setMinimum(float(minimo))
        if self._shares.value() < float(minimo):
            self._shares.setValue(float(minimo))
        self._price.setDecimals(_decimals_of(tick))
        self._price.setSingleStep(float(tick))
        self._price.setRange(float(tick), float(Decimal(1) - tick))
        self._propose_price()

    def _propose_price(self) -> None:
        """Propone un precio desde el libro, mientras siga siendo una propuesta.

        Se propone desde lo que el mercado está pidiendo **ahora** —el mejor
        `ask` si se compra, el mejor `bid` si se vende— y si no hay libro se cae
        al precio publicado del resultado. En los dos casos se baja al múltiplo
        del salto.

        Que respete el precio escrito a mano es lo que permite que el libro
        llegue **después**: pedirlo es una lectura de red, así que el precio sólo
        puede afinarse cuando la respuesta llega, y pisar entonces lo que alguien
        acaba de teclear sería peor que no afinarlo.
        """
        if not self._price_auto:
            return
        market = self._market()
        outcome = self._selected_outcome()
        if market is None or outcome is None:
            return
        tick = market.tick_size or Decimal("0.01")
        referencia = outcome.price
        if self._depth is not None:
            del_libro = self._depth.best_ask if self._is_buy() else self._depth.best_bid
            if del_libro is not None:
                referencia = del_libro
        # Se bloquean las señales para que el `setValue` de la propuesta no se
        # confunda con una edición del usuario y la convierta en definitiva.
        self._price.blockSignals(True)
        self._price.setValue(float(_round_to_tick(referencia, tick)))
        self._price.blockSignals(False)
        self._price_auto = True

    def _side_value(self) -> PredictionSide:
        """El lado elegido, **reconstruido** desde el desplegable.

        `currentData()` no devuelve el `PredictionSide` que se le dio: Qt guarda
        el dato del ítem como texto y lo devuelve como texto, y `PredictionSide`
        es un `StrEnum`, así que compararlo con `is` daba falso **siempre** y el
        panel cotizaba toda compra por el lado de la venta. Reconstruirlo aquí
        funciona con las dos formas —el enum o su texto— y deja la comparación en
        un solo sitio.
        """
        return PredictionSide(self._side.currentData())

    def _is_buy(self) -> bool:
        return self._side_value() is PredictionSide.BUY

    def _collateral(self) -> Token | None:
        """El token con el que liquida el recinto, o `None` si no se puede saber."""
        market = self._market()
        if market is None:
            return None
        try:
            return self._container.registry.prediction_planner().collateral_for(market)
        except Exception:
            return None

    def _cost(self) -> Decimal:
        return Decimal(str(self._price.value())) * Decimal(str(self._shares.value()))

    def _refresh_order_state(self) -> None:
        market = self._market()
        motivos = self._order_blockers()
        self._submit_btn.setEnabled(market is not None and not motivos)
        # El motivo se enseña sólo cuando ya hay un mercado elegido: antes de eso
        # el botón apagado no dice nada que el usuario no sepa —no ha elegido
        # nada—, y un aviso permanente en rojo se aprende a ignorar.
        self._order_note.setText(
            ""
            if market is None or not motivos
            else "No se puede publicar: " + _motivos(motivos)
        )

        puede = self._container.guard.allows(Capability.BROADCAST_TX)
        self._order_chip.set_state(
            f"MODO {self._container.guard.mode.label}",
            COLOR_SUCCESS if puede else COLOR_MUTED,
        )

        if market is None:
            self._order_cost.setText("—")
            self._order_payout.setText("")
            self._book_label.setText("")
            return

        colateral = self._collateral()
        symbol = colateral.symbol if colateral is not None else "colateral"
        self._order_cost.setText(f"{'Pagas' if self._is_buy() else 'Cobras'} {self._cost()} {symbol}")
        self._order_payout.setText(
            f"{self._shares.value():g} participaciones pagan {self._shares.value():g} "
            f"{symbol} si acierta el resultado, y 0 si no."
        )
        self._refresh_book_label()

    def _refresh_book_label(self) -> None:
        """El libro real del resultado, y si el tamaño pedido cabe en él."""
        if self._depth is None:
            self._book_label.setText("")
            return
        compra = self._depth.best_bid
        venta = self._depth.best_ask
        partes = [
            f"Libro: compra {compra if compra is not None else 'sin compras'} · "
            f"venta {venta if venta is not None else 'sin ventas'}"
        ]
        shares = Decimal(str(self._shares.value()))
        if self._is_buy():
            coste = self._depth.cost_to_buy(shares)
            partes.append(
                "cruzar ahora costaría "
                + (f"{coste}" if coste is not None else "más de lo que hay en venta")
            )
        else:
            ingreso = self._depth.proceeds_to_sell(shares)
            partes.append(
                "cruzar ahora daría "
                + (f"{ingreso}" if ingreso is not None else "más de lo que hay en compra")
            )
        self._book_label.setText(" · ".join(partes))

    def _order_blockers(self) -> tuple[str, ...]:
        """Todo lo que falta para poder publicar. Vacío significa que sí.

        Se devuelven **todos** los motivos y no el primero, por la misma razón
        que en la pestaña de swap: son condiciones distintas —el modo se cambia
        aquí, el interruptor en el fichero, el motor y la cartera aparte— y
        descubrirlas de una en una hace pensar que la aplicación está rota en vez
        de a medio configurar.

        Mira lo mismo que mira `PlacePredictionOrder` antes de firmar, y en el
        mismo orden: con el mercado, el modo, el interruptor, el colateral, el
        motor y la cartera. Si esta lista dijera que sí y el caso de uso dijera
        que no, la interfaz estaría prometiendo algo que no cumple.
        """
        container = self._container
        market = self._market()
        if market is None:
            return ("no hay ningún mercado seleccionado",)

        # Lo primero, si el mercado **es** operable: un mercado leído sin los
        # datos que hacen falta para firmar no lo arregla ninguna configuración.
        motivos = list(market.tradeability_blockers())

        if not container.guard.mode.grants(Capability.BROADCAST_TX):
            motivos.append(
                f"el modo {container.guard.mode.label} no permite emitir "
                "(cambia a EJECUCIÓN)"
            )
        if not container.policy.limits.enabled:
            motivos.append(
                "la ejecución está apagada (`enabled = true` bajo `[execution]` "
                "en config.toml)"
            )

        limites = container.policy.limits
        cadena = market.venue.chain
        if cadena not in limites.allowed_chains:
            motivos.append(
                f"la red «{cadena}» no está en `allowed_chains` "
                f"(ahora declara: {', '.join(sorted(limites.allowed_chains)) or 'nada'})"
            )

        planner = None
        try:
            planner = container.registry.prediction_planner()
        except Exception as error:
            # El registro ya explica esto con nombre propio —el motor activo lee
            # mercados pero no sabe operar—, y aquí se repite tal cual en vez de
            # resumirlo: el texto del registro dice qué hacer.
            motivos.append(str(error))
        if planner is not None:
            try:
                colateral = planner.collateral_for(market)
            except Exception as error:
                motivos.append(f"no se pudo determinar el colateral del recinto: {error}")
            else:
                if colateral.symbol not in limites.allowed_tokens:
                    # Se dice también **qué hay declarado**: el error más común
                    # no es olvidar la lista, es escribir en ella un nombre que
                    # ningún token tiene.
                    motivos.append(
                        f"el colateral «{colateral.symbol}» no está en "
                        f"`allowed_tokens` (ahora declara: "
                        f"{', '.join(sorted(limites.allowed_tokens)) or 'nada'}; "
                        f"añádelo en config.toml)"
                    )
            engine_id = planner.manifest.engine_id
            if engine_id not in limites.allowed_engines:
                motivos.append(
                    f"el motor «{engine_id}» no está en `allowed_engines` "
                    f"(ahora declara: "
                    f"{', '.join(sorted(limites.allowed_engines)) or 'nada'})"
                )
        if not container.keys.available():
            motivos.append("no hay cartera configurada, así que no hay con qué firmar")

        # Y por último la orden concreta que se está escribiendo. Va al final
        # porque sólo tiene sentido preguntarla cuando lo demás ya está: si
        # falta la cartera, el precio da igual.
        motivos.extend(self._order_input_blockers(market))
        return tuple(motivos)

    def _order_input_blockers(self, market: PredictionMarket) -> list[str]:
        """Lo que hace inválida **esta** orden, no la configuración."""
        motivos: list[str] = []
        shares = Decimal(str(self._shares.value()))
        precio = Decimal(str(self._price.value()))
        minimo = market.min_order_size
        if minimo is not None and shares < minimo:
            motivos.append(f"el mercado pide un mínimo de {minimo} participaciones")
        if market.tick_size is not None and precio % market.tick_size != 0:
            motivos.append(
                f"el precio {precio} no es múltiplo del salto "
                f"{market.tick_size} del mercado"
            )
        if self._selected_outcome() is None:
            motivos.append("el resultado elegido no está entre los del mercado")
        if self._collateral() is None:
            motivos.append("el colateral del recinto no tiene decimales conocidos")
        return motivos

    async def _do_load_book(self) -> None:
        """Lee el libro real del resultado elegido. Lectura pública, sin claves."""
        outcome = self._selected_outcome()
        if outcome is None or outcome.token_id is None:
            self._depth = None
            self._book_label.setText("")
            return
        token_id = outcome.token_id
        try:
            engine = self._container.registry.active_prediction()
            depth = await engine.book(token_id)
        except Exception as error:
            self._depth = None
            self._book_label.setText(f"No se pudo leer el libro: {error}")
            return
        # La respuesta puede llegar después de que el usuario haya cambiado de
        # resultado. Guardarla entonces dejaría la profundidad de un token
        # enseñada bajo el nombre de otro, que es peor que no enseñarla.
        elegido = self._selected_outcome()
        if elegido is None or elegido.token_id != token_id:
            return
        self._depth = depth
        # El libro llegó después de que el precio se propusiera, así que ahora es
        # cuando se puede afinar. `_propose_price` respeta lo que se haya escrito.
        self._apply_market_limits()
        self._refresh_order_state()

    def _on_submit(self) -> None:
        market = self._market()
        label = self._outcome_label()
        if market is None or label is None:
            self._order_status.setText("Selecciona un mercado y un resultado.")
            return
        motivos = self._order_blockers()
        if motivos:
            # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el
            # modo se puede cambiar desde la barra mientras esta pestaña está
            # abierta, y un botón sólo se repinta cuando algo lo repinta.
            self._order_status.setText("No se puede publicar: " + _motivos(motivos))
            return
        wallet = self._container.keys.address()
        if wallet is None:
            self._order_status.setText("No hay cartera configurada, así que no hay quién firme.")
            return
        self._submit_btn.setEnabled(False)
        self._order_status.setText("Construyendo y firmando la orden…")
        spawn(self._do_submit(market, label, wallet))

    async def _do_submit(
        self, market: PredictionMarket, label: str, wallet: str
    ) -> None:
        """Publica la orden. El destinatario es **la propia cartera que firma**.

        En un recinto de predicción la orden la hace quien la firma: el
        comprador recibe las participaciones y el vendedor el colateral, y los
        dos son el `maker` de la orden. Un destinatario distinto no es una opción
        que este código no ofrezca —es que el protocolo no lo tiene—, así que no
        se pregunta: se enseña cuál es.
        """
        try:
            submitted = await self._container.place_prediction_order(
                market,
                outcome_label=label,
                side=self._side_value(),
                size=Decimal(str(self._shares.value())),
                price=Decimal(str(self._price.value())),
                recipient=wallet,
            )
        except Exception as error:
            self._order_status.setText(f"Error: {error}")
        else:
            self._order_status.setText(
                f"Publicada · identificador {submitted.order_id} · estado "
                f"{submitted.status}. No es una transacción: está en el libro y "
                "se puede cancelar mientras nadie la haya cruzado."
            )
        finally:
            self._refresh_order_state()
            # El libro cambió con la orden, y la orden pudo cruzarse sola.
            spawn(self._do_load_book())

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
            set_empty(
                self._baskets,
                self._baskets_empty,
                "Busca mercados arriba: las cestas se calculan sobre los que "
                "hayan salido.",
            )
            self._weigh_baskets()
            return
        found = self._container.find_prediction_opportunities.from_reports(
            self._reports,
            min_edge_bps=BasisPoints(self._min_edge.value()),
        )
        for opportunity in found:
            self._add_basket_row(opportunity)
        self._baskets.resizeRowsToContents()
        # El motivo nombra el umbral **con su valor**: es el único de los dos
        # que el usuario puede mover, y sin el número tendría que adivinar
        # cuánto bajarlo para que aparezca algo.
        set_empty(
            self._baskets,
            self._baskets_empty,
            "Ninguna de las cestas calculadas llega al umbral de "
            f"{self._min_edge.value()} bps sobre los {len(self._reports)} mercados "
            "leídos: en todas, comprar los resultados cuesta lo mismo o más que "
            "el pago garantizado. Baja el umbral para ver las que quedan cerca.",
        )
        self._weigh_baskets()

    def _weigh_baskets(self) -> None:
        """Le da a las cestas el alto que les toca **según lo que hay dentro**.

        Media pantalla reservada para un aviso de una línea es peor que el aviso
        solo: el hueco no dice nada y le roba el sitio a la tabla de mercados,
        que es donde está el trabajo. Cuando hay filas, la sección vuelve a
        pedir su mitad.
        """
        hay = self._baskets.rowCount() > 0
        self._root_layout.setStretchFactor(self._baskets, 1 if hay else 0)
        # El rótulo nunca estira: su sitio es justo el de su texto.
        self._root_layout.setStretchFactor(self._baskets_empty, 0)

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
