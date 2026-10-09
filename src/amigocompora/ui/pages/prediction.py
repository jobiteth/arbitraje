"""Pestaña: Mercados de predicción — mirarlos **y operarlos**.

Cuatro zonas. Arriba la búsqueda; en medio, a la izquierda la tabla de mercados y a
la derecha la tarjeta de orden; debajo la wallet de depósito, con su saldo y sus
posiciones; y abajo las cestas con margen y, debajo, lo que ya se resolvió y se
puede cobrar.

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

### Y la wallet por la que pasa todo

Polymarket ya no opera desde una cartera normal: las compras y las ventas van por
una **deposit wallet**, un contrato que custodia el colateral y las participaciones
y que controla la misma clave. La tarjeta de la wallet enseña las dos cosas
—saldo y posiciones con su precio medio y su resultado— y deja añadirle saldo por
los dos caminos que de verdad hay: la retirada de siempre con el destino ya
puesto, o un envío externo a su dirección (que se copia o se enseña en QR). Cada
fila de su tabla vuelve a la tarjeta de orden con «Vender» o «Comprar más»:
**cargar no firma** —deja el mercado, el resultado, el lado y el tamaño puestos— y
revisar y publicar sigue siendo lo de arriba, con sus comprobaciones. Lo que
todavía no se puede es cobrar sus posiciones resueltas —eso pide un lote del
relayer que la aplicación aún no construye— y la fila lo dice apagando sus botones
con el motivo, en vez de ofrecer algo que no existe.

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

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QDialog,
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
from amigocompora.app.usecases.read_settlement_wallet import (
    CHAIN_KEY as SETTLEMENT_WALLET_CHAIN,
)
from amigocompora.app.usecases.read_settlement_wallet import (
    SettlementWalletView,
)
from amigocompora.app.usecases.withdraw import WALLET_ENGINE_ID
from amigocompora.domain.addresses import shorten
from amigocompora.domain.execution import ANY_TOKEN
from amigocompora.domain.models import (
    MarketDepth,
    MarketOutcome,
    PredictionMarket,
    PredictionPosition,
    PredictionSide,
    Token,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.ui.pages.wallet import DepositDialog, WithdrawDialog
from amigocompora.ui.theme import (
    COLOR_ACCENT,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_SUCCESS,
)
from amigocompora.ui.widgets import (
    Card,
    Chip,
    Field,
    ScrollArea,
    divider,
    format_amount,
    set_empty,
    spawn,
)

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

#: Lo mismo para la tabla de posiciones de la wallet de depósito, que lleva una
#: columna más de cifras y por eso pide unos píxeles más de alto.
_WALLET_TABLE_HEIGHT = 170


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


def _pnl_text(posicion: PredictionPosition) -> str:
    """Lo ganado o perdido de una posición, tal como lo publica la fuente.

    La cifra **no se recalcula** aquí a partir del precio actual y el precio
    medio: la fuente ya publica el resultado y su porcentaje, y una segunda
    cuenta que discrepe de la primera serían dos «cuánto llevo ganado» en la
    misma pantalla. Si la fuente no lo publica, se dice con un guion; lo que no
    se hace es inventar un cero.
    """
    if posicion.cash_pnl is None:
        return "—"
    signo = "+" if posicion.cash_pnl > 0 else ""
    texto = f"{signo}{posicion.cash_pnl:f}"
    if posicion.percent_pnl is not None:
        texto += f" ({signo}{posicion.percent_pnl:.1f} %)"
    return texto


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

        lay = ScrollArea.fill(self, spacing=10).body()
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

        # Los tres pasos, a la vista y con el que toca encendido. La pantalla tiene
        # tres —elegir mercado, decidir lado y precio, publicar— y no enseñaba
        # ninguno: la tarjeta de orden parecía un formulario suelto que no hacía
        # nada, y la pregunta «no sé cómo usar esto» se contestaba con la nada. El
        # paso encendido sale del estado real, no de un contador que alguien tenga
        # que ir moviendo: ver `_refresh_steps`.
        self._steps: list[Chip] = []
        pasos = QHBoxLayout()
        pasos.setSpacing(6)
        for texto in ("1 · Elige un mercado", "2 · Lado y precio", "3 · Publica la orden"):
            chip = Chip(texto, COLOR_MUTED)
            self._steps.append(chip)
            pasos.addWidget(chip)
        pasos.addStretch()
        self._step_hint = QLabel("")
        self._step_hint.setObjectName("hint")
        self._step_hint.setWordWrap(True)
        pasos.addWidget(self._step_hint, 1)
        lay.addLayout(pasos)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Pregunta", "Favorito", "Total %", "Overround", "Coherente", "Cierra"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(True)
        # Un mínimo para que la lista se lea: antes la tabla se repartía el alto que
        # quedara y acababa en una franja de dos filas.
        self._table.setMinimumHeight(240)

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

        # --- La wallet de depósito ---------------------------------------------
        # Va entre la tarjeta de orden y las cestas porque su trabajo es
        # **volver** a la de orden: cada fila de su tabla carga ese mercado
        # arriba —vender, comprar más— y la de arriba es la que opera. Con la
        # wallet al final de la pestaña, cada acción sería un viaje de ida y
        # vuelta por la pantalla.
        lay.addWidget(self._build_wallet_card())

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
        self._baskets.setMinimumHeight(160)
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
        #: El mercado cargado en la tarjeta de orden, o `None`. Es lo que la
        #: tarjeta está enseñando **y lo que se firmaría**: las filas de la wallet
        #: también cargan la tarjeta, y para entonces la selección de la tabla de
        #: mercados puede estar vacía o ser otra. Ver `_market`.
        self._chosen_market: PredictionMarket | None = None
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
        #: Lo último leído de la wallet de depósito, o `None` mientras no se haya
        #: leído. Mismo motivo que `_positions` para vivir aquí: es una lectura de
        #: red, y repintar no es motivo para salir a la red.
        self._wallet_view: SettlementWalletView | None = None
        self._wallet_read = False
        self._wallet_error: str | None = None
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
        # Y la de la wallet de depósito, por el mismo motivo: su dirección se
        # deriva sin red y su botón de añadir saldo sale apagado **con el motivo
        # escrito** si falta modo o configuración.
        self._fill_wallet_card()
        self._refresh_wallet_state()
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
        self._chosen_market = None
        self._depth = None
        self._price_auto = True
        self._outcome.blockSignals(True)
        self._outcome.clear()
        self._outcome.blockSignals(False)
        self._rebuild_outcome_buttons(None)
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
        self._chosen_market = market
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
        self._rebuild_outcome_buttons(market)

        self._apply_market_limits()
        self._refresh_order_state()
        spawn(self._do_load_book())

    # ------------------------------------------------------------------ #
    # Operar: la tarjeta de orden
    # ------------------------------------------------------------------ #
    def _build_order_card(self) -> Card:
        """La tarjeta que convierte un mercado leído en una orden firmable.

        Tiene el aspecto de un panel de operación: el lado, el resultado, el precio
        y las participaciones se eligen con botones. Los combos `_outcome` y `_side`
        siguen siendo la fuente de verdad: los botones sólo los reflejan, así que la
        lógica de precio, libro y límites no cambia.
        """
        card = Card("Operar", subtitle="— orden límite")
        # Ancho mínimo de verdad y **sin tope**: el tope de 460 px era el que
        # aplastaba los campos y dejaba la tarjeta ilegible justo donde se escriben
        # las cifras que se van a firmar.
        card.setMinimumWidth(400)

        self._order_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._order_chip)

        self._order_market = QLabel("Selecciona un mercado de la tabla.")
        self._order_market.setObjectName("hint")
        self._order_market.setWordWrap(True)
        card.body().addWidget(self._order_market)

        self._outcome = QComboBox()
        self._outcome.currentIndexChanged.connect(self._on_outcome_changed)
        self._side = QComboBox()
        self._side.addItem("Comprar", PredictionSide.BUY)
        self._side.addItem("Vender", PredictionSide.SELL)
        self._side.currentIndexChanged.connect(self._on_side_changed)
        card.body().addWidget(self._outcome)
        card.body().addWidget(self._side)
        self._outcome.hide()
        self._side.hide()

        self._side_buttons = QButtonGroup(card)
        self._side_buttons.setExclusive(True)
        fila_lado = QHBoxLayout()
        fila_lado.setSpacing(6)
        for indice, texto in enumerate(("Comprar", "Vender")):
            boton = QPushButton(texto)
            boton.setObjectName("segment")
            boton.setCheckable(True)
            boton.setToolTip(
                "Comprar o vender el resultado elegido. Son operaciones distintas: "
                "vender «Sí» y comprar «No» no son lo mismo."
            )
            boton.clicked.connect(lambda _c=False, i=indice: self._side.setCurrentIndex(i))
            self._side_buttons.addButton(boton, indice)
            fila_lado.addWidget(boton)
        fila_lado.addStretch(1)
        limite = QLabel("Límite")
        limite.setObjectName("hint")
        fila_lado.addWidget(limite)
        card.add_row(fila_lado)
        self._mark_side_buttons()

        self._outcome_row = QHBoxLayout()
        self._outcome_row.setSpacing(8)
        self._outcome_buttons: list[QPushButton] = []
        card.add_row(self._outcome_row)

        self._price = QDoubleSpinBox()
        self._price.setDecimals(2)
        self._price.setRange(0.01, 0.99)
        self._price.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._price.setToolTip(
            "Precio máximo al comprar (mínimo al vender) por participación. Se "
            "propone desde el libro real y se ajusta al salto del mercado. La "
            "orden queda en el libro hasta que la canceles: no se ejecuta sola "
            "ni caduca."
        )
        self._price.valueChanged.connect(self._on_price_edited)
        fila_precio = QHBoxLayout()
        fila_precio.setSpacing(6)
        fila_precio.addWidget(_boton_paso("−", lambda: self._price.stepDown()))
        fila_precio.addWidget(self._price, 1)
        fila_precio.addWidget(_boton_paso("+", lambda: self._price.stepUp()))
        campo_precio = Field("PRECIO LÍMITE")
        campo_precio.add(_contenedor(fila_precio), 1)

        self._shares = QDoubleSpinBox()
        self._shares.setDecimals(2)
        self._shares.setRange(1.0, 1_000_000.0)
        self._shares.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._shares.setToolTip(
            "Participaciones. Cada una paga 1 del colateral si el resultado "
            "ocurre, y 0 si no."
        )
        self._shares.valueChanged.connect(self._on_order_edited)
        fila_acciones = QHBoxLayout()
        fila_acciones.setSpacing(6)
        fila_acciones.addWidget(self._shares, 1)
        fila_chips = QHBoxLayout()
        fila_chips.setSpacing(6)
        for delta in (-100, -10, 10, 100, 150):
            chip = QPushButton(f"{delta:+d}".replace("-", "−"))
            chip.setObjectName("secondary")
            chip.clicked.connect(lambda _c=False, d=delta: self._adjust_shares(d))
            fila_chips.addWidget(chip)
        campo_acciones = Field("ACCIONES")
        campo_acciones.add(_contenedor(fila_acciones), 1)
        campo_acciones.add(_contenedor(fila_chips), 1)

        fila2 = QHBoxLayout()
        fila2.setSpacing(8)
        fila2.addWidget(campo_precio, 1)
        fila2.addWidget(campo_acciones, 1)
        card.add_row(fila2)

        fila_vence = QHBoxLayout()
        vence = QLabel("Vence")
        vence.setObjectName("hint")
        fila_vence.addWidget(vence)
        fila_vence.addStretch(1)
        nunca = QLabel("Nunca")
        nunca.setToolTip("La orden queda en el libro hasta que la canceles.")
        fila_vence.addWidget(nunca)
        card.add_row(fila_vence)

        card.body().addWidget(divider())

        # Lo que cuesta y lo que paga, en la misma línea y con las dos cifras
        # juntas: el coste sin el pago potencial es la mitad de la información
        # que hace falta para decidir.
        total = QLabel("Total")
        total.setObjectName("hint")
        card.body().addWidget(total)
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

        self._submit_btn = QPushButton("Realizar orden de compra")
        self._submit_btn.setObjectName("primary")
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

    def _rebuild_outcome_buttons(self, market: PredictionMarket | None) -> None:
        while self._outcome_row.count():
            item = self._outcome_row.takeAt(0)
            viejo = item.widget() if item is not None else None
            if viejo is not None:
                viejo.deleteLater()
        self._outcome_buttons = []
        if market is None:
            return
        for indice, outcome in enumerate(market.outcomes):
            boton = QPushButton(f"{outcome.label}  {outcome.implied_percent:.0f}¢")
            boton.setObjectName("outcome")
            boton.setCheckable(True)
            boton.clicked.connect(lambda _c=False, i=indice: self._outcome.setCurrentIndex(i))
            self._outcome_row.addWidget(boton)
            self._outcome_buttons.append(boton)
        self._mark_outcome_buttons()

    def _mark_outcome_buttons(self) -> None:
        actual = self._outcome.currentIndex()
        for indice, boton in enumerate(self._outcome_buttons):
            boton.setChecked(indice == actual)

    def _mark_side_buttons(self) -> None:
        actual = self._side.currentIndex()
        for indice in (0, 1):
            self._side_buttons.button(indice).setChecked(indice == actual)

    def _adjust_shares(self, delta: int) -> None:
        self._shares.setValue(max(self._shares.minimum(), self._shares.value() + delta))

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
                if (
                    ANY_TOKEN not in limites.allowed_tokens
                    and colateral.symbol.upper() not in limites.allowed_tokens
                ):
                    # Se dice también **qué hay declarado**: el error más común no
                    # es olvidar la lista, es escribir en ella un nombre que ningún
                    # token tiene. Y la comparación es por mayúsculas, como
                    # `check_token`: el símbolo del catálogo es `pUSD` y la lista
                    # llega normalizada.
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
    # La wallet de depósito: verla y fondearla
    # ------------------------------------------------------------------ #
    def _build_wallet_card(self) -> Card:
        """La tarjeta de la deposit wallet: su saldo, sus posiciones y su fondeo.

        Es la cuenta por la que Polymarket opera desde 2026, y la pestaña no la
        enseñaba en ninguna parte: se podía comprar y vender sin ver qué quedaba
        dentro. Aquí se lee —lectura pública, sin claves—, se le añade saldo por
        los dos caminos que de verdad hay y cada posición vuelve a la tarjeta de
        orden de arriba.

        **Nada de lo que hay aquí firma.** Leer es público, y añadir saldo abre la
        retirada de siempre —`WithdrawFunds`, con su modo, sus topes y su
        confirmación— con el destino ya puesto: la tarjeta no tiene un camino
        propio hacia la firma, sólo enseña el que ya existe.
        """
        card = Card("Wallet de depósito", subtitle="— la cuenta que opera en Polymarket")
        card.setMinimumWidth(400)

        self._wallet_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._wallet_chip)

        intro = QLabel(
            "Polymarket ya no opera desde una cartera normal: tus compras y ventas "
            "van por una <b>deposit wallet</b>, un contrato que custodia el "
            "colateral y las participaciones y que controla tu clave. No necesita "
            "gas —los permisos y los envíos los manda el relayer— y sólo liquida "
            "con el colateral del recinto (pUSD): mandar otro token ahí lo deja "
            "encerrado."
        )
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        card.body().addWidget(intro)

        fila_dir = QHBoxLayout()
        fila_dir.setSpacing(6)
        rotulo = QLabel("Dirección")
        rotulo.setObjectName("hint")
        fila_dir.addWidget(rotulo)
        self._wallet_address = QLineEdit()
        self._wallet_address.setReadOnly(True)
        self._wallet_address.setCursorPosition(0)
        self._wallet_address.setToolTip(
            "La dirección de tu deposit wallet, derivada de tu cartera. Se conoce "
            "antes de desplegarla: recibir en ella no depende de ningún permiso ni "
            "de credenciales del relayer."
        )
        fila_dir.addWidget(self._wallet_address, 1)
        self._wallet_copy_btn = QPushButton("Copiar")
        self._wallet_copy_btn.setObjectName("secondary")
        self._wallet_copy_btn.setToolTip(
            "Copia la dirección para pegarla donde vayas a enviar el saldo."
        )
        self._wallet_copy_btn.clicked.connect(self._on_copy_wallet)
        fila_dir.addWidget(self._wallet_copy_btn)
        self._wallet_qr_btn = QPushButton("Ver QR")
        self._wallet_qr_btn.setObjectName("secondary")
        self._wallet_qr_btn.setToolTip(
            "El código para escanear desde una cartera o un exchange. Se enseña la "
            "dirección sola: la red la eliges tú al enviar."
        )
        self._wallet_qr_btn.clicked.connect(self._on_show_wallet_qr)
        fila_dir.addWidget(self._wallet_qr_btn)
        card.add_row(fila_dir)

        # Que la dirección no se pueda derivar —el motor activo puede no saber
        # planificar— se dice aquí, junto al hueco: una caja vacía sin motivo se
        # lee como que no hay wallet, y sí la hay.
        self._wallet_address_error = QLabel("")
        self._wallet_address_error.setWordWrap(True)
        self._wallet_address_error.setStyleSheet(f"color: {COLOR_DANGER};")
        card.body().addWidget(self._wallet_address_error)

        saldo = QHBoxLayout()
        saldo.setSpacing(8)
        etiqueta = QLabel("Saldo")
        etiqueta.setObjectName("hint")
        saldo.addWidget(etiqueta)
        self._wallet_balance = QLabel("—")
        self._wallet_balance.setObjectName("bigNumber")
        saldo.addWidget(self._wallet_balance)
        self._wallet_total = QLabel("")
        self._wallet_total.setObjectName("hint")
        self._wallet_total.setWordWrap(True)
        saldo.addWidget(self._wallet_total, 1)
        card.add_row(saldo)

        fila = QHBoxLayout()
        fila.setSpacing(8)
        self._wallet_read_btn = QPushButton("Leer la wallet")
        self._wallet_read_btn.setObjectName("secondary")
        self._wallet_read_btn.setToolTip(
            "Lee el saldo y las posiciones de la wallet de depósito. Es una "
            "lectura pública: no firma ni emite nada."
        )
        self._wallet_read_btn.clicked.connect(lambda: spawn(self._do_read_wallet()))
        fila.addWidget(self._wallet_read_btn, 1)
        self._wallet_add_btn = QPushButton("Añadir saldo…")
        self._wallet_add_btn.setObjectName("secondary")
        self._wallet_add_btn.setToolTip(
            "Retira colateral de tu cartera hacia la wallet, con el destino ya "
            "puesto: es la retirada de siempre, con su confirmación y sus topes. "
            "Para recibir desde fuera, usa «Ver QR» o «Copiar»."
        )
        self._wallet_add_btn.clicked.connect(self._on_add_funds)
        fila.addWidget(self._wallet_add_btn, 1)
        card.add_row(fila)

        self._wallet_table = QTableWidget(0, 7)
        self._wallet_table.setHorizontalHeaderLabels(
            [
                "Mercado",
                "Resultado",
                "Participaciones",
                "Precio medio",
                "Valor",
                "Ganancia",
                "",
            ]
        )
        self._wallet_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self._wallet_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._wallet_table.setAlternatingRowColors(True)
        self._wallet_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._wallet_table.setWordWrap(True)
        self._wallet_table.setMinimumHeight(_WALLET_TABLE_HEIGHT)
        cabecera = self._wallet_table.horizontalHeaderItem(5)
        if cabecera is not None:
            cabecera.setToolTip(
                "Lo ganado (o perdido) frente a lo pagado, según la fuente: el "
                "signo y el color lo dicen todo. Un guion es «la fuente no lo "
                "publica», nunca un cero."
            )

        #: El rótulo que ocupa el sitio de la tabla mientras está vacía, con la
        #: misma altura que ella para que el alto no salte. Mismo recurso que en
        #: las otras dos tablas, y distingue «todavía no has leído» de «leíste y
        #: no hay nada», que se arreglan de forma distinta.
        self._wallet_empty = QLabel("")
        self._wallet_empty.setObjectName("empty")
        self._wallet_empty.setWordWrap(True)
        self._wallet_empty.setAlignment(Qt.AlignCenter)
        self._wallet_empty.setMinimumHeight(_WALLET_TABLE_HEIGHT)
        card.body().addWidget(self._wallet_table)
        card.body().addWidget(self._wallet_empty)

        self._wallet_note = QLabel("")
        self._wallet_note.setWordWrap(True)
        self._wallet_note.setStyleSheet(f"color: {COLOR_DANGER};")
        card.body().addWidget(self._wallet_note)

        self._wallet_status = QLabel("")
        self._wallet_status.setObjectName("hint")
        self._wallet_status.setWordWrap(True)
        card.body().addWidget(self._wallet_status)
        return card

    def _fill_wallet_card(self) -> None:
        """Pinta el saldo, la tabla de posiciones y su rótulo vacío."""
        view = self._wallet_view
        self._wallet_table.setRowCount(0)
        if view is None:
            self._wallet_balance.setText("—")
            self._wallet_total.setText("")
        else:
            self._wallet_balance.setText(
                f"{format_amount(view.balance.as_decimal())} {view.collateral.symbol}"
            )
            self._wallet_total.setText(self._wallet_total_text(view))
            self._fill_wallet_rows(view)
        set_empty(self._wallet_table, self._wallet_empty, self._wallet_empty_text())

    def _fill_wallet_rows(self, view: SettlementWalletView) -> None:
        """Una fila por posición, con sus cifras y sus dos botones de acción.

        El «Valor» se calcula —participaciones por precio actual— porque la
        fuente publica las dos cifras y una multiplicación no es una segunda
        verdad; la «Ganancia», en cambio, se copia tal cual: recalcularla
        exigiría el histórico de compras, que esta lectura no trae.
        """
        self._wallet_table.setRowCount(len(view.positions))
        for fila, posicion in enumerate(view.positions):
            valor = (
                posicion.shares * posicion.cur_price
                if posicion.cur_price is not None
                else None
            )
            celdas = (
                posicion.question,
                posicion.outcome_label,
                f"{posicion.shares:f}",
                "—" if posicion.avg_price is None else f"{posicion.avg_price:f}",
                "—" if valor is None else f"{valor:f}",
            )
            for columna, texto in enumerate(celdas):
                self._wallet_table.setItem(fila, columna, QTableWidgetItem(texto))
            ganancia = QTableWidgetItem(_pnl_text(posicion))
            if posicion.cash_pnl is not None:
                # El color repite el signo del número: se lee de un vistazo qué
                # posiciones van a favor y cuáles en contra.
                ganancia.setForeground(
                    QColor(COLOR_SUCCESS if posicion.cash_pnl >= 0 else COLOR_DANGER)
                )
            self._wallet_table.setItem(fila, 5, ganancia)
            self._wallet_table.setCellWidget(fila, 6, self._wallet_row_actions(posicion))
        self._wallet_table.resizeRowsToContents()

    def _wallet_row_actions(self, posicion: PredictionPosition) -> QWidget:
        """Los dos botones de una fila: «Vender» y «Comprar más».

        Cargan la tarjeta de orden y **no firman**: lo que hacen es traer el
        mercado de la posición —por su `conditionId`, que es una lectura de red— y
        dejar el resultado, el lado y el tamaño puestos para revisarlos. La firma
        sigue siendo el botón de arriba, con sus comprobaciones y su diálogo.

        Una fila ya resuelta sale con los dos apagados y el motivo escrito: sus
        participaciones se cobran, no se operan —y el cobro desde la wallet
        todavía no existe, porque pide un lote del relayer que la aplicación aún
        no construye—.
        """
        contenedor = QWidget()
        caja = QHBoxLayout(contenedor)
        caja.setContentsMargins(0, 0, 0, 0)
        caja.setSpacing(4)
        vender = QPushButton("Vender")
        vender.setObjectName("secondary")
        comprar = QPushButton("Comprar más")
        comprar.setObjectName("secondary")
        if posicion.redeemable:
            for boton in (vender, comprar):
                boton.setEnabled(False)
                boton.setToolTip(
                    "El mercado ya resolvió: estas participaciones se cobran, no se "
                    "operan. El cobro desde la wallet de depósito todavía no está "
                    "en la aplicación."
                )
        else:
            vender.setToolTip(
                "Carga esta posición en la tarjeta de orden de arriba para "
                "venderla. No firma nada: revisa el precio y publica allí."
            )
            comprar.setToolTip(
                "Carga este mercado en la tarjeta de orden de arriba para comprar "
                "más de este resultado. No firma nada."
            )
            vender.clicked.connect(
                lambda _c=False, p=posicion: self._on_position_action(
                    p, PredictionSide.SELL
                )
            )
            comprar.clicked.connect(
                lambda _c=False, p=posicion: self._on_position_action(
                    p, PredictionSide.BUY
                )
            )
        caja.addWidget(vender)
        caja.addWidget(comprar)
        return contenedor

    @staticmethod
    def _wallet_total_text(view: SettlementWalletView) -> str:
        """La suma de lo ganado, y **de qué está sumada**.

        Sólo suma lo que la fuente publica: si una posición no trae resultado, no
        se le inventa un cero. Y cuando falta alguna se dice, porque una suma
        incompleta presentada como total es una cifra falsa.
        """
        conocidos = [
            posicion.cash_pnl
            for posicion in view.positions
            if posicion.cash_pnl is not None
        ]
        if not conocidos:
            return ""
        total = sum(conocidos, Decimal(0))
        texto = (
            f"En conjunto: {'+' if total > 0 else ''}{total:f} {view.collateral.symbol}"
        )
        if len(conocidos) < len(view.positions):
            texto += (
                f" (suma de {len(conocidos)} de {len(view.positions)} posiciones: "
                "la fuente no publica el resto)"
            )
        return texto

    def _wallet_empty_text(self) -> str:
        """Por qué la tabla está vacía. Nunca es un «error» a secas."""
        if self._wallet_error is not None:
            return f"No se pudo leer la wallet: {self._wallet_error}"
        view = self._wallet_view
        if view is None:
            return (
                "Pulsa «Leer la wallet» para ver su saldo y sus posiciones. Es "
                "una lectura pública: no firma ni emite nada."
            )
        if not view.positions:
            if view.balance.is_zero:
                return (
                    "La wallet no tiene saldo ni posiciones abiertas. Añádele "
                    "saldo con «Añadir saldo…» —desde tu cartera o desde fuera— y "
                    "opera en la tarjeta de orden de arriba."
                )
            return (
                f"Tiene {format_amount(view.balance.as_decimal())} "
                f"{view.collateral.symbol} y ninguna posición abierta: lo que "
                "compres en la tarjeta de orden sale de ese saldo, y las "
                "posiciones aparecen aquí mientras el mercado no resuelva."
            )
        return ""

    async def _do_read_wallet(self) -> None:
        """Lee saldo y posiciones de la wallet. Lectura pública, sin claves.

        Se leen **todas** las posiciones y no sólo las cobrables —lo contrario
        que en la tarjeta de cobro—: aquí lo que interesa es lo abierto, para
        poder operar sobre ello, y lo resuelto sólo aparece para decir que no se
        opera.
        """
        self._wallet_read_btn.setEnabled(False)
        self._wallet_status.setText("Leyendo la wallet de depósito…")
        try:
            view = await self._container.read_settlement_wallet()
        except Exception as error:
            # Un fallo de lectura no se disfraza de «no hay nada»: el rótulo
            # vacío lo cuenta con el motivo entero.
            self._wallet_view = None
            self._wallet_error = str(error)
        else:
            self._wallet_view = view
            self._wallet_error = None
            self._wallet_status.setText("")
        finally:
            self._wallet_read = True
            self._wallet_read_btn.setEnabled(True)
        self._fill_wallet_card()

    def _refresh_wallet_state(self) -> None:
        """Pinta la dirección, el botón de añadir saldo y su motivo.

        La dirección se **deriva** en cada repintado —es pura, sin red— y no se
        guarda: si la clave cambia en Credenciales, el repintado llega antes que
        cualquier otra cosa y la dirección tiene que ser la nueva, no la de la
        cartera anterior.
        """
        try:
            direccion = self._container.read_settlement_wallet.address()
        except Exception as error:
            direccion = ""
            self._wallet_address_error.setText(str(error))
        else:
            self._wallet_address_error.setText("")
        self._wallet_address.setText(direccion)
        self._wallet_address.setCursorPosition(0)
        self._wallet_copy_btn.setEnabled(bool(direccion))
        self._wallet_qr_btn.setEnabled(bool(direccion))

        motivos = self._wallet_add_blockers()
        self._wallet_add_btn.setEnabled(not motivos and bool(direccion))
        # Aquí el motivo se enseña **siempre** que falte algo, y no sólo después
        # de leer: añadir saldo es lo que estrena la wallet, así que un botón
        # apagado sin motivo se leería como que la función no existe. Es la regla
        # de las otras dos tarjetas, con el «cuándo» adaptado a que aquí no hay
        # nada que esperar a tener.
        self._wallet_note.setText(
            "" if not motivos else "No se puede añadir saldo: " + _motivos(motivos)
        )
        puede = self._container.guard.allows(Capability.BROADCAST_TX)
        self._wallet_chip.set_state(
            f"MODO {self._container.guard.mode.label}",
            COLOR_SUCCESS if puede else COLOR_MUTED,
        )

    def _wallet_add_blockers(self) -> tuple[str, ...]:
        """Todo lo que falta para poder añadir saldo. Vacío significa que sí.

        Mira lo mismo que mira `WithdrawFunds` antes de emitir —el modo, el
        interruptor, la red, el token, el motor y la cartera— y en el mismo orden,
        porque añadir saldo **es** una retirada: la de la pestaña de cartera, con
        el destino puesto. Si esta lista dijera que sí y el caso de uso dijera que
        no, la interfaz estaría prometiendo algo que no cumple.
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
            motivos.append(
                "no hay cartera configurada, así que no hay desde dónde retirar"
            )
            # Sin cartera no hay más que comprobar: el destino y el origen salen
            # de ella, y sin ella no hay ni una cosa ni la otra.
            return tuple(motivos)

        limites = container.policy.limits
        if SETTLEMENT_WALLET_CHAIN not in limites.allowed_chains:
            motivos.append(
                f"la red «{SETTLEMENT_WALLET_CHAIN}» no está en `allowed_chains` "
                f"(ahora declara: {', '.join(sorted(limites.allowed_chains)) or 'nada'})"
            )
        try:
            colateral = container.read_settlement_wallet.collateral()
        except Exception as error:
            motivos.append(f"no se pudo determinar el colateral del recinto: {error}")
        else:
            # Por mayúsculas, como `ExecutionLimits.check_token`: la lista llega
            # normalizada y el símbolo del catálogo es `pUSD`.
            if (
                ANY_TOKEN not in limites.allowed_tokens
                and colateral.symbol.upper() not in limites.allowed_tokens
            ):
                motivos.append(
                    f"el colateral «{colateral.symbol}» no está en "
                    f"`allowed_tokens` (ahora declara: "
                    f"{', '.join(sorted(limites.allowed_tokens)) or 'nada'}; "
                    f"añádelo en config.toml)"
                )
        if WALLET_ENGINE_ID not in limites.allowed_engines:
            motivos.append(
                f"el motor «{WALLET_ENGINE_ID}» no está en `allowed_engines` "
                f"(ahora declara: {', '.join(sorted(limites.allowed_engines)) or 'nada'})"
            )
        return tuple(motivos)

    def _on_copy_wallet(self) -> None:
        """Copia la dirección de la wallet. Sin efectos: es texto al portapapeles."""
        direccion = self._wallet_address.text().strip()
        if not direccion:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(direccion)
        self._wallet_status.setText(
            "Dirección copiada. Se queda en el portapapeles hasta que algo la "
            "sustituya."
        )

    def _on_show_wallet_qr(self) -> None:
        """El QR de la dirección, con lo que hay que mandarle y lo que no."""
        direccion = self._wallet_address.text().strip()
        if not direccion:
            return
        DepositDialog(
            direccion,
            SETTLEMENT_WALLET_CHAIN,
            self,
            note=(
                "Esta es tu deposit wallet de Polymarket: sólo liquida con el "
                "<b>colateral del recinto (pUSD)</b>, y cualquier otro token que "
                "llegue aquí queda encerrado —la aplicación todavía no sabe "
                "sacarlo—. Manda sólo pUSD por la red Polygon."
            ),
        ).exec()

    def _on_add_funds(self) -> None:
        """Retira de la cartera propia a la wallet, con el destino ya puesto.

        El diálogo y el caso de uso son **los de la pestaña de cartera**, sin una
        copia: aquí sólo se prefija el destino y se limita la lista al colateral.
        Ofrecer otros tokens sería ofrecer fondos que quedarían encerrados: la
        wallet los recibiría y todavía no hay forma de sacarlos.
        """
        direccion = self._wallet_address.text().strip()
        if not direccion:
            return
        try:
            colateral = self._container.read_settlement_wallet.collateral()
        except Exception as error:
            self._wallet_status.setText(
                f"No se puede añadir saldo sin saber cuál es el colateral del "
                f"recinto: {error}"
            )
            return
        dialogo = WithdrawDialog(
            (colateral,),
            default=colateral,
            owner=self._container.keys.address() or "",
            recipient=direccion,
            parent=self,
        )
        if dialogo.exec() != QDialog.Accepted:
            return
        token, texto, destino = dialogo.chosen()
        try:
            amount = token.amount(texto)
        except Exception as error:
            self._wallet_status.setText(f"Importe no válido: {error}")
            return
        self._wallet_status.setText(
            f"Retirando {amount} de {token.symbol} hacia la wallet… cada paso pide "
            "su confirmación."
        )
        spawn(self._do_add_funds(token, amount, destino))

    async def _do_add_funds(
        self, token: Token, amount: TokenAmount, recipient: str
    ) -> None:
        """Ejecuta la retirada y relee la wallet para que el saldo no mienta.

        La relectura va antes de escribir el resultado, y no después: su propio
        mensaje borraría el de la retirada. Y existe porque ver el saldo viejo
        justo después de fondear es la forma más rápida de fondear dos veces.
        """
        try:
            receipt = await self._container.withdraw_funds(
                token, amount, recipient=recipient
            )
        except Exception as error:
            # El motivo entero a la pantalla. Un caso de uso que explica por qué
            # no firma —«no se pudo valorar», «no cabe el gas»— pierde todo su
            # valor si la interfaz lo resume en «error».
            self._wallet_status.setText(f"No se retiró nada: {error}")
            return
        await self._do_read_wallet()
        self._wallet_status.setText(
            f"Retirada emitida: {amount} de {token.symbol} → {shorten(recipient)}. "
            f"Transacción {receipt.tx_hash}."
        )

    def _on_position_action(
        self, posicion: PredictionPosition, side: PredictionSide
    ) -> None:
        """Carga la posición en la tarjeta de orden. Sólo carga: no firma."""
        spawn(self._do_load_position_market(posicion, side))

    async def _do_load_position_market(
        self, posicion: PredictionPosition, side: PredictionSide
    ) -> None:
        """Trae el mercado de la posición y deja la tarjeta lista para revisarla.

        El mercado se busca por el `conditionId` y no por el nombre: el nombre se
        repite entre mercados, y cargar «el que se le parece» sería enseñar un
        mercado distinto de aquel en el que se tiene la posición —y firmar sobre
        él—. Nada se toca hasta que la carga puede terminar: cuando algo la
        impide, se dice con la tarjeta como estaba.
        """
        self._order_status.setText(
            "Buscando el mercado de la posición… es una lectura pública."
        )
        try:
            engine = self._container.registry.active_prediction()
            market = await engine.market_by_condition(posicion.condition_id)
        except Exception as error:
            self._order_status.setText(
                f"No se pudo cargar el mercado de esa posición: {error}"
            )
            return

        resultado = next(
            (o for o in market.outcomes if o.token_id == posicion.token_id), None
        )
        if resultado is None:
            self._order_status.setText(
                f"El mercado «{market.question}» ya no publica el resultado "
                f"«{posicion.outcome_label}» que tienes. No se cargó nada: operar "
                "sobre otro resultado no sería operar sobre tu posición."
            )
            return

        if side is PredictionSide.SELL:
            # Suelo a dos decimales —los del campo— y **hacia abajo**: el widget
            # redondearía hacia arriba y propondría vender más participaciones de
            # las que hay.
            cantidad = posicion.shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            minimo = market.min_order_size or Decimal(1)
            if cantidad < minimo:
                self._order_status.setText(
                    f"Tu posición es de {posicion.shares:f} participaciones y el "
                    f"mercado pide un mínimo de {minimo} por orden: la venta entera "
                    "no se puede publicar. «Comprar más» sí está disponible para "
                    "llegar al mínimo."
                )
                return
        else:
            # Comprar más se propone por el mínimo del mercado: es el compromiso
            # más pequeño que existe, y ampliarlo es del usuario.
            cantidad = market.min_order_size or Decimal(1)

        self._table.clearSelection()
        self._detail.setText("")
        self._fill_order_card(market)
        indice = self._outcome.findData(resultado.label)
        if indice >= 0:
            self._outcome.setCurrentIndex(indice)
        self._shares.setValue(float(cantidad))
        self._side.setCurrentIndex(0 if side is PredictionSide.BUY else 1)
        self._order_status.setText(
            f"Cargada desde tu posición de la wallet: {posicion.shares:f} "
            f"{posicion.outcome_label}. Revisa el precio contra el libro y publica "
            "— cargarla no firma nada."
        )

    # ------------------------------------------------------------------ #
    # Estado de la orden
    # ------------------------------------------------------------------ #
    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo: los botones que firman.

        Público porque quien sabe que el modo ha cambiado es la ventana, no esta
        pestaña: `ModeGuard.subscribe` avisa a quien se apunte, y la pestaña no se
        apunta sola. Existe en vez de que la ventana llame a los privados porque un
        método privado de otra clase no es una interfaz, es un atajo.

        Y son **los tres**: el de publicar, el de cobrar y el de añadir saldo a la
        wallet. El modo decide si se puede emitir, así que sin esto los tres se
        quedan como estaban y el usuario descubre el cambio al pulsarlos —que es
        justo lo que enseña a desconfiar de los botones—.
        """
        self._refresh_order_state()
        self._refresh_redeem_state()
        self._refresh_wallet_state()

    def _market(self) -> PredictionMarket | None:
        """El mercado cargado en la tarjeta, o `None` si no hay ninguno.

        No se deriva de la selección de la tabla: las filas de la wallet de
        depósito también cargan la tarjeta —«Vender», «Comprar más»— y para
        entonces la selección de la tabla puede estar vacía. Lo que manda es lo
        que la tarjeta está enseñando, que es exactamente lo que se firmaría.
        """
        return self._chosen_market

    def _outcome_label(self) -> str | None:
        label = self._outcome.currentData()
        return label if isinstance(label, str) else None

    def _selected_outcome(self) -> MarketOutcome | None:
        market = self._market()
        label = self._outcome_label()
        return None if market is None or label is None else market.outcome(label)

    def _on_outcome_changed(self) -> None:
        """Cambiar de resultado cambia de libro: es otro token, otra profundidad."""
        self._mark_outcome_buttons()
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
        self._mark_side_buttons()
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
        self._refresh_steps()
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

    def _refresh_steps(self) -> None:
        """Enciende el paso en el que está el usuario, y dice qué falta para el siguiente.

        Se lee del estado de verdad —¿hay mercado elegido?, ¿hay libro leído?— y no
        de un contador que alguien tenga que ir moviendo: un contador hay que
        acordarse de tocarlo en cada camino que cambia de paso, y el día que se
        olvide uno la pantalla dirá «paso 1» con una orden ya cargada.

        El libro es lo que separa el paso 2 del 3 porque es lo que separa un precio
        inventado de uno que se puede cruzar: hasta que no llega, el campo de precio
        lleva la propuesta del panel y no lo que hay en el libro.
        """
        if self._market() is None:
            actual = 0
            falta = "Empieza por la tabla: elige el mercado que quieras operar."
        elif self._depth is None:
            actual = 1
            falta = "Elige resultado, lado y participaciones; el libro del resultado se está leyendo."
        else:
            actual = 2
            falta = "Revisa el precio contra el libro y publica la orden."
        for indice, chip in enumerate(self._steps):
            chip.set_color(COLOR_ACCENT if indice == actual else COLOR_MUTED)
        self._step_hint.setText(falta)

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
                if (
                    ANY_TOKEN not in limites.allowed_tokens
                    and colateral.symbol not in limites.allowed_tokens
                ):
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
            # Comprar baja el saldo de la wallet y sube sus posiciones —vender, al
            # revés—: releerla evita que la tarjeta de abajo siga enseñando el
            # saldo de antes de operar. Sólo si ya se había leído: una lectura que
            # nadie ha pedido no se gasta por publicar una orden.
            if self._wallet_read:
                spawn(self._do_read_wallet())
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


def _boton_paso(texto: str, accion: Callable[[], None]) -> QPushButton:
    boton = QPushButton(texto)
    boton.setObjectName("stepper")
    boton.clicked.connect(lambda _c=False: accion())
    return boton


def _contenedor(fila: QHBoxLayout) -> QWidget:
    contenedor = QWidget()
    contenedor.setLayout(fila)
    return contenedor
