"""Pestaña **Swap**: convertir, enviar, depositar, y firmar si se puede.

La pantalla tiene tres zonas, y el orden no es decorativo: es el orden en que se
toman las decisiones.

1. **La tarjeta de intercambio** (`ui/pages/swap_card.py`). Cuenta, modo y red
   arriba; las tres operaciones —intercambiar, enviar, depositar— en un segmento;
   y debajo las dos patas del par con su token, su importe y su saldo.
2. **Las rutas.** Lo que contestaron los motores, con la comisión y el impacto de
   cada uno, y la ruta elegida escrita en una línea.
3. **Lo que se puede hacer con ella.** Un solo botón, *Ejecutar*, que hace la
   operación entera —preparar, pedir los permisos que falten, estimar el coste,
   firmar y emitir— narrando cada fase en tiempo real hasta quedar en
   «✓ Completado». Cada transacción real sigue pidiendo su propio «sí» en su
   diálogo: lo que se quitó no es esa regla, es tener un botón por fase. Al
   terminar, el panel de transacciones enseña qué se emitió, qué se rechazó y
   con qué hash.

### Qué es de esta página y qué es de la tarjeta

Esta página es la dueña del **estado**: la comparación de rutas, el payload
preparado, la ejecución, y la decisión de qué se puede firmar. La tarjeta es la
vista que lo pide y lo enseña, y no sabe nada de casos de uso. La separación no es
estética: permite probar la tarjeta sin red y sin contenedor, y deja el fichero
que firma lejos del que coloca campos.

### Sobre la dirección del par

El par se arma con lo que **entregas** como base y lo que **recibes** como
cotización, y la cantidad se escribe siempre en la base. Hubo antes un desplegable
«Comprar / Vender» que elegía qué pata era cuál: obligaba a traducir mentalmente
«vender WETH» a «WETH → USDC» en cada operación, y con esa traducción repartida por
la página la dirección acababa invertida en alguna vista y el importe se firmaba al
revés.
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.execute_swap import StepState, StepUpdate, SwapStep
from amigocompora.app.usecases.scan_opportunities import DEFAULT_MIN_NET_BPS
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
)
from amigocompora.domain.models import (
    BroadcastReceipt,
    Opportunity,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints
from amigocompora.infra.config import config_file
from amigocompora.infra.config_writer import set_execution_slippage_bps
from amigocompora.ui.execution_gate import (
    cap_blockers,
    execution_blockers,
    recipient_candidates,
)
from amigocompora.ui.pages.bridges import BridgesSection
from amigocompora.ui.pages.swap_card import SwapCard, position_of
from amigocompora.ui.pages.token_picker import TokenPickerDialog
from amigocompora.ui.receipt_dialog import SwapReceiptDialog
from amigocompora.ui.route_list import RouteDetail, RouteList
from amigocompora.ui.slippage_dialog import SlippageDialog
from amigocompora.ui.swap_steps import SwapStepsPanel
from amigocompora.ui.theme import COLOR_DANGER
from amigocompora.ui.wallet_state import WalletBalances
from amigocompora.ui.widgets import (
    Card,
    ScrollArea,
    set_empty,
    spawn,
    tokens_for_chain,
)

#: Lo que dice el botón de operar cuando no hay nada en marcha. Es también el
#: texto al que vuelve tras un rechazo o un fallo: el intento no consumió nada
#: —lo ya concedido se reaprovecha—, así que se puede volver a pulsar.
_EXEC_LABEL = "Ejecutar: firmar y emitir…"

#: Lo que dice cuando la operación llegó al final por el camino bueno. Se queda
#: puesto hasta que el usuario elige otra ruta o pulsa «Empezar de nuevo»: el
#: desenlace era de la ruta que se firmó, y decirlo al lado de otra engañaría.
_DONE_LABEL = "✓ Completado"

#: Lo que dice la tabla de rutas cuando aún no se ha pedido nada. Se escribe aquí y
#: no en el widget para que el mismo texto valga para el estado inicial y para el
#: que queda tras un error: los dos son «no hay rutas», y decir cosas distintas en
#: los dos sitios es la forma de que uno de los dos acabe mintiendo.
#: Cuánto se espera tras el último cambio antes de cotizar. Lo bastante corto para que
#: parezca inmediato y lo bastante largo para no lanzar una petición por cada tecla.
_PAUSA_COTIZACION_MS = 600

_SIN_COTIZAR = (
    "Todavía no has cotizado. Elige la red y las dos patas, y pulsa «Cotizar "
    "rutas»: cotizar no firma ni emite nada."
)


class PricesPage(QWidget):
    #: Un token nuevo —pegado por su dirección— que ya está en el almacén. La
    #: ventana lo lleva a la lista de la cartera para que la incluya sin releer.
    token_added = Signal(object)

    def __init__(
        self,
        container: Container,
        parent: QWidget | None = None,
        *,
        balances: WalletBalances | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: PriceComparison | None = None
        #: Las cotizaciones **que la tabla está enseñando ahora mismo**, en el
        #: mismo orden que sus filas. No siempre es `comparison.ranked`: con
        #: `[ui] hide_quote_only_routes` las que no se pueden firmar no llegan a
        #: la tabla, y la selección tiene que apuntar a lo que sí se ve.
        self._shown_quotes: tuple[Quote, ...] = ()
        #: La ruta cuyo coste de red se acaba de estimar. El panel de detalle
        #: reinicia su coste al cambiar de ruta —la cifra era de otra— salvo
        #: cuando la ruta elegida es ésta, que es el caso que se da al volver de
        #: `_do_execute` con la estimación recién puesta por el paso ESTIMATE.
        self._cost_quote: Quote | None = None
        #: La operación en marcha. Mientras esté puesta, el botón narra y no se
        #: puede volver a pulsar; el estado vive aquí y no en el botón porque
        #: quien lo repinta —`_on_quote_selected`— corre en cada cambio de ruta.
        self._exec_running = False
        #: La operación terminó por el camino bueno y sigue en pantalla. El botón
        #: queda en «✓ Completado» —apagado— hasta que se elija otra ruta o se
        #: pulse «Empezar de nuevo».
        self._exec_done = False
        #: La ruta que produjo ese desenlace. Es la identidad que decide si el
        #: estado terminal sigue siendo cierto para la ruta elegida ahora.
        self._executed_quote: Quote | None = None
        #: La confirmación de la última operación emitida. Se guarda aquí para que no
        #: la recoja el recolector de basura antes de que se vea.
        self._recibo: SwapReceiptDialog | None = None
        #: Cotización automática: se lanza cuando el usuario deja de cambiar el importe
        #: o el par, no en cada tecla. `_quote_seq` marca cuál es la cotización vigente:
        #: si llega otra antes de que acabe la anterior, la vieja se descarta —y la
        #: tarea en vuelo se cancela—, porque sus resultados ya no describen nada.
        self._quote_timer = QTimer(self)
        self._quote_timer.setSingleShot(True)
        self._quote_timer.setInterval(_PAUSA_COTIZACION_MS)
        self._quote_timer.timeout.connect(self._on_quote)
        self._quote_seq = 0
        #: La cotización en vuelo, para poder cancelarla cuando se pide otra. Una
        #: petición que ya no interesa no debe seguir gastando cuota de los motores
        #: ni llegar tarde a pintar un par que el usuario ya cambió.
        self._quote_task: asyncio.Task[None] | None = None
        #: Los saldos, compartidos con la lista de cartera. Una sola caché para
        #: las dos vistas: con dos, elegir un token dispararía dos peticiones al
        #: mismo nodo por el mismo dato, y en Solana el nodo es público y va
        #: racionado por ventana. La ventana pasa la misma instancia a las dos; si
        #: nadie la pasa —una `PricesPage` suelta, que es como la montan las
        #: pruebas— se crea una propia, que funciona igual y sólo deja de compartir.
        self._balances = balances or WalletBalances(container.read_wallet)

        # Dos columnas: a la izquierda lo que se opera, a la derecha lo que se
        # tiene. Apilarlas —que es lo que había— obligaba a bajar y subir para
        # comprobar un saldo que se está a punto de firmar.
        columnas = QHBoxLayout(self)
        columnas.setContentsMargins(0, 0, 0, 0)
        columnas.setSpacing(12)

        self._scroll = ScrollArea(spacing=12)
        columnas.addWidget(self._scroll, 1)
        lay = self._scroll.body()

        self._card = SwapCard(container, self._balances)
        self._converter = self._card
        lay.addWidget(self._card)

        # El bloque de la operación va **entre la tarjeta del swap y las rutas**,
        # centrado en la columna: es el botón que convierte lo de arriba en una
        # operación real, así que vive pegado a lo que usa —el importe, el par,
        # la ruta elegida— y por encima de la lista de rutas, que es información
        # para elegir. Antes vivía dentro de la tarjeta de rutas, debajo del
        # panel de detalle, a un scroll del importe que iba a firmarse.
        lay.addWidget(self._build_execution())

        #: El panel lateral, a la derecha y con ancho fijo. Lo llena la ventana
        #: con la lista de la cartera —ver `set_side_panel`—; mientras nadie lo
        #: use no ocupa nada, así que quien monte esta página sola la sigue viendo
        #: como antes.
        # El panel va dentro de un armazón que se desplaza. Sin él, la cartera —que
        # crece con sus filas y sus botones— se cortaba por la parte de abajo de la
        # pestaña: lo que no cabe en el alto se pierde si nada permite bajar.
        self._side = ScrollArea(spacing=12)
        self._side.setObjectName("sidePanel")
        # 420 px: la lista de tokens tiene seis columnas y los botones de la cartera
        # dos por fila. A 340 px la tabla y los botones se recortaban; a 420 entran
        # enteros. Con este ancho la tarjeta de intercambio sigue cabiendo en los
        # 1240 px mínimos de la ventana.
        self._side.setFixedWidth(420)
        self._side_lay = self._side.body()
        self._side.setVisible(False)
        columnas.addWidget(self._side, 0)

        # Las dos tablas miden lo que miden sus filas, así que las tarjetas se
        # quedan con su alto natural y el hueco sobrante cae al final de la página
        # en vez de repartirse por dentro de cada tarjeta, que es donde antes
        # aparecía como un rectángulo gris debajo de los botones sin nada dentro.
        lay.addWidget(self._build_routes())
        lay.addWidget(self._build_opportunities())
        # Y por último cruzar de red, que es la operación más larga de las tres y la
        # que menos se hace. La ventana la lleva a su propia pestaña con
        # `take_bridges`; si nadie la reclama —una `PricesPage` suelta, que es como
        # la montan las pruebas— se queda aquí, que es donde ha vivido siempre.
        self._bridges = BridgesSection(container, balances=self._balances)
        lay.addWidget(self._bridges)
        # Con peso 1 y no 0: `addStretch()` sin argumento crea un espaciador con
        # peso **cero**, que no absorbe nada, y el sobrante acababa repartido entre
        # las tarjetas que no saben usarlo.
        lay.addStretch(1)

        self._routes.currentRowChanged.connect(self._on_quote_selected)
        self._chain.currentIndexChanged.connect(self._on_chain_changed)
        self._base.currentIndexChanged.connect(self._update_legs_labels)
        self._contra.currentIndexChanged.connect(self._update_legs_labels)
        # La tarjeta pide; la página decide. Es la misma separación que ya había
        # entre la vista y el caso de uso, un escalón más arriba.
        self._card.quote_requested.connect(self._on_quote)
        # Cotizar sin tocar nada: cualquier cambio del importe programa una cotización.
        # Las patas y la red la programan desde `_update_legs_labels` y `_on_chain_changed`.
        self._card.amount.valueChanged.connect(lambda _valor: self._schedule_quote())
        self._card.invert_requested.connect(self._on_invert)
        self._card.token_picked.connect(self._on_token_picked)
        self._card.balances_requested.connect(self._refresh_balances)
        self._card.send_requested.connect(self._on_send)
        # El engranaje de la tarjeta pide configurar el deslizamiento; abrir el
        # diálogo, aplicarlo y guardarlo es de esta página, que es la que tiene
        # el valor vivo y el camino a `config.toml`.
        self._card.slippage_requested.connect(self._on_slippage)
        self._balances.subscribe(self._on_balances_changed)

        self._refresh_tokens()
        self._card.reload_send_tokens()
        # El estado inicial de los botones y del chip de modo: sin esto la tarjeta
        # abre con el chip vacío hasta que alguien cotice, y el modo es justo lo que
        # hay que ver **antes** de ponerse a cotizar.
        self._on_quote_selected()
        self._clear_results(_SIN_COTIZAR)
        self.refresh_execution_state()

    # ------------------------------------------------------------------ #
    # Lo que la ventana necesita para componer la pantalla
    # ------------------------------------------------------------------ #
    def set_side_panel(self, widget: QWidget) -> None:
        """Coloca un bloque en la columna de la derecha: la lista de la cartera.

        Lo usa la ventana para poner ahí la cartera, que es lo que se mira antes de
        escribir un importe. Va **al lado** y no debajo porque la pantalla tiene dos
        cosas a la vez —lo que se opera y lo que se tiene— y apilarlas obligaba a
        bajar y subir para comprobar un saldo que se está a punto de firmar.

        Un widget vive en un solo padre, así que esto **mueve** lo que se le pase:
        no se puede tener a la vez aquí y en su pestaña.
        """
        self._side_lay.addWidget(widget)
        self._side.setVisible(True)

    def take_bridges(self) -> BridgesSection:
        """Suelta la sección de puentes para que la coloque quien compone las pestañas.

        Un widget vive en un solo sitio, así que no puede estar a la vez dentro del
        scroll de cotizaciones y en una pestaña propia. La sección sigue siendo de
        esta página —comparte su modo y su estado de ejecución, y
        `refresh_execution_state` la repinta—; lo único que cambia es quién la
        coloca. Si nadie la reclama, se queda donde está.
        """
        self._scroll.body().removeWidget(self._bridges)
        self._bridges.setParent(None)
        return self._bridges

    def scroll_to_converter(self) -> None:
        """Sube la vista hasta la tarjeta y pone el cursor en el importe.

        Es lo que sustituye al cambio de pestaña que hacía «Intercambiar este
        token»: la cartera está en la misma pantalla, así que lo que falta no es
        irse a otro sitio, es volver al sitio donde se escribe —y con el cursor ya
        dentro, que ahorra el clic que sigue a haber elegido un token.
        """
        self._scroll.scroll_to(self._converter)
        self._amount.setFocus()

    # ------------------------------------------------------------------ #
    # La tarjeta, delegada
    #
    # Estas propiedades existen para que la página siga siendo la dueña del estado
    # sin que sus llamantes tengan que saber cómo está montada la tarjeta por
    # dentro: la ventana, los puentes y las pruebas siguen hablando de `_chain`,
    # `_legs()` o `_amount` exactamente igual que antes del rediseño.
    # ------------------------------------------------------------------ #
    @property
    def _base(self) -> QComboBox:
        return self._card.base

    @property
    def _contra(self) -> QComboBox:
        return self._card.contra

    @property
    def _chain(self) -> QComboBox:
        return self._card.chain

    @property
    def _amount(self) -> QDoubleSpinBox:
        return self._card.amount

    @property
    def _unit(self) -> QLabel:
        return self._card.unit

    @property
    def _balance(self) -> QLabel:
        return self._card.balance

    @property
    def _max_btn(self) -> QPushButton:
        return self._card.max_btn

    @property
    def _pair(self) -> QLabel:
        return self._card.pair

    @property
    def _status(self) -> QLabel:
        return self._card.status

    @property
    def _invert_btn(self) -> QPushButton:
        return self._card.invert_btn

    def _legs(self) -> tuple[Token | None, Token | None]:
        """Las dos patas del par, en el orden en que se mueven."""
        return self._card.legs()

    # ------------------------------------------------------------------ #
    # El estado compartido de saldos
    # ------------------------------------------------------------------ #
    def _on_balances_changed(self, chain_key: str) -> None:
        """Repinta cuando llega una lectura. Sólo pinta: no vuelve a pedir."""
        del chain_key
        self._card.paint()

    def _refresh_balances(self) -> None:
        """Relee las redes que se están mirando. Es el botón «Saldos»."""
        owner = self._owner()
        for cadena in self._painted_chains():
            self._balances.refresh(cadena, owner=owner)
        self._card.paint()

    def _painted_chains(self) -> tuple[str, ...]:
        """Las redes que la tarjeta está enseñando: la elegida y la de cada pata."""
        cadenas = [self._chain.currentData()]
        entrega, recibe = self._legs()
        enviado = self._card.selected_send_token()
        for token in (entrega, recibe, enviado):
            if token is not None:
                cadenas.append(token.chain)
        return tuple(dict.fromkeys(c for c in cadenas if isinstance(c, str) and c))

    def _owner(self) -> str:
        """La dirección de la cartera que firma, o cadena vacía si no hay clave."""
        return self._container.keys.address() or ""

    # ------------------------------------------------------------------ #
    # Zona 2 — las rutas y lo que se puede hacer con ellas
    # ------------------------------------------------------------------ #
    def _build_execution(self) -> QWidget:
        """El bloque de la operación: botón, ruta elegida, aviso y transacciones.

        Va entre la tarjeta del swap y la de rutas, centrado en la columna, y lo
        que se firma, el botón que lo firma, por qué está apagado y lo que pasó
        de verdad cuentan una sola historia: por eso viven juntos y no dentro de
        la tarjeta de rutas, que es donde se elige lo de arriba.
        """
        bloque = QWidget()
        lay = QVBoxLayout(bloque)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # Un solo botón de operar, centrado: es el único que firma y lo que hace
        # convierte el formulario de arriba en una operación real. Antes había
        # tres —preparar, guardar y ejecutar— y había que saber cuál tocaba:
        # preparar no firmaba, guardar sólo exportaba, y ejecutar era el de
        # verdad. Ahora el botón hace la operación entera y va contando cada
        # fase en su propio texto; el «sí» de cada transacción sigue viviendo en
        # su diálogo, que es donde tiene que estar.
        actions = QHBoxLayout()
        actions.addStretch()
        self._exec_btn = QPushButton(_EXEC_LABEL)
        self._exec_btn.setObjectName("danger")
        self._exec_btn.setToolTip(
            "Prepara la operación, pide los permisos que falten, estima el coste, "
            "firma y emite. Es irreversible y sale dinero de tu cartera; cada "
            "transacción pedirá su propia confirmación."
        )
        self._exec_btn.clicked.connect(self._on_execute)
        self._exec_btn.setEnabled(False)
        actions.addWidget(self._exec_btn)

        # «Empezar de nuevo» no firma nada: limpia la lista de transacciones de
        # la operación y deja el botón como estaba. Nace oculto y aparece en
        # cuanto hay algo que limpiar.
        self._new_btn = QPushButton("Empezar de nuevo")
        self._new_btn.setObjectName("secondary")
        self._new_btn.setToolTip(
            "Limpia la lista de transacciones de la operación y vuelve al estado "
            "inicial. No firma ni emite nada."
        )
        self._new_btn.clicked.connect(self._on_start_over)
        self._new_btn.setVisible(False)
        actions.addWidget(self._new_btn)
        actions.addStretch()
        lay.addLayout(actions)

        # La ruta elegida, escrita, justo debajo del botón: es lo que el botón va
        # a usar, y sin esta línea habría que deducir cuál de las filas
        # resaltadas es. De una línea, aunque el texto sea largo: una `QLabel`
        # con `wordWrap` publica un alto natural calculado para varias líneas y
        # luego se dibuja en una, y esa diferencia quedaba como un hueco muerto
        # al final de la tarjeta. El texto entero, por si se recorta, va en el
        # tooltip.
        self._chosen = QLabel("Selecciona una ruta para ejecutarla.")
        self._chosen.setObjectName("hint")
        self._chosen.setWordWrap(False)
        lay.addWidget(self._chosen)

        # Por qué «Ejecutar» está apagado, en la misma línea y no escondido en un
        # tooltip. Un botón apagado sin motivo es lo que empuja a buscar la forma de
        # saltárselo, así que aquí se dice qué falta exactamente — y se dice
        # **antes** de que alguien lo intente, no cuando falla.
        #
        # Y cuando lo que falta es el modo, al lado va el atajo para cambiarlo: el
        # mensaje decía «cambia a EJECUCIÓN» y dejaba al usuario buscando dónde. El
        # modo sólo lo cambia una acción explícita del usuario, y un botón que se
        # pulsa lo es; lo que no puede haber es que lo cambie el programa.
        note_row = QHBoxLayout()
        self._exec_note = QLabel("")
        self._exec_note.setWordWrap(True)
        self._exec_note.setStyleSheet(f"color: {COLOR_DANGER};")
        note_row.addWidget(self._exec_note, stretch=1)

        self._upgrade_btn = QPushButton("")
        self._upgrade_btn.setObjectName("link")
        self._upgrade_btn.clicked.connect(self._on_upgrade_mode)
        self._upgrade_btn.setVisible(False)
        note_row.addWidget(self._upgrade_btn)
        lay.addLayout(note_row)

        # Las transacciones de la operación, debajo de los botones y del aviso:
        # una fila por transacción real, con su estado. Nace oculto y sólo
        # aparece cuando hay algo que contar.
        self._steps = SwapStepsPanel()
        lay.addWidget(self._steps)
        return bloque

    def _build_routes(self) -> QWidget:
        card = Card("Rutas encontradas")
        # Por defecto, 4 decimales: una cifra de 18 decimales no cabe en la tabla y
        # no ayuda a comparar. La casilla devuelve el valor completo cuando hace falta.
        self._todos_decimales = QCheckBox("Todos los decimales")
        self._todos_decimales.toggled.connect(lambda _marcado: self._apply_decimales())
        card.header.addWidget(self._todos_decimales)
        self._route_count = QLabel("")
        self._route_count.setObjectName("hint")
        card.header.addWidget(self._route_count)

        # Una tabla sin filas es un rectángulo gris grande que no dice nada: no se
        # distingue «todavía no has pedido nada» de «pediste y no hay ruta». El
        # rótulo ocupa el sitio de la lista mientras está vacía y lo explica.
        self._routes_empty = QLabel("")
        self._routes_empty.setObjectName("empty")
        self._routes_empty.setWordWrap(True)
        self._routes_empty.setAlignment(Qt.AlignCenter)
        card.body().addWidget(self._routes_empty, stretch=1)

        # Las rutas, como tarjetas de dos líneas en un listbox: lo que se compara
        # —venue y lo que se recibe— arriba y el desglose debajo. La segunda línea
        # se recorta antes que envolverse: una fila por ruta, para poder comparar
        # de un vistazo, y lo que no cabe se recupera en el tooltip.
        self._routes = RouteList()
        card.body().addWidget(self._routes, stretch=1)

        # Y debajo, la ruta elegida en filas de etiqueta y valor: es la que se va
        # a preparar o firmar, y sus datos no compiten con los de las demás. El
        # panel es también donde vive el coste de red estimado, que no se puede
        # enseñar en la tarjeta porque no se conoce hasta preparar.
        self._detail = RouteDetail()
        # Nace oculto, como la lista: sin rutas que elegir, el rótulo que ocupa el
        # sitio ya dice qué hacer y el panel repetiría la instrucción debajo.
        self._detail.setVisible(False)
        card.body().addWidget(self._detail)

        # Nace plegada: sin cotización no hay nada que enseñar, y desplegada
        # empujaba el resto fuera de la pantalla en la primera visita —que es justo
        # cuando el usuario viene a intercambiar, no a leer una lista vacía—. Se
        # abre sola en cuanto llega una cotización (`_fill_routes`).
        self._routes_card = card
        card.set_collapsible(expanded=False)
        return _hugs_content(card)

    def _build_opportunities(self) -> QWidget:
        # El umbral se interpola desde el motor y no se escribe a mano: el rótulo y
        # el estado vacío dicen el mismo número, o el que mienta será uno de los dos
        # el día que el motor lo cambie.
        card = Card(
            "Oportunidades", subtitle=f"— diferencial neto ≥ {DEFAULT_MIN_NET_BPS} bps"
        )
        self._opps_empty = QLabel("")
        self._opps_empty.setObjectName("empty")
        self._opps_empty.setWordWrap(True)
        card.body().addWidget(self._opps_empty)

        self._opp_table = QTableWidget(0, 5)
        self._opp_table.setHorizontalHeaderLabels(
            ["Mejor", "Referencia", "Spread bruto", "Spread neto", "Accionable"]
        )
        self._opp_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._opp_table.setAlternatingRowColors(True)
        self._opp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._opp_table.setShowGrid(False)
        self._opp_table.verticalHeader().setVisible(False)
        self._opp_table.setWordWrap(False)
        card.body().addWidget(self._opp_table)
        # Plegada por el mismo motivo que las rutas —informa, no se opera— y se abre
        # sola cuando hay una cotización cuyo veredicto acompañar.
        self._opps_card = card
        card.set_collapsible(expanded=False)
        return _hugs_content(card)

    # ------------------------------------------------------------------ #
    # Los tokens de las patas
    # ------------------------------------------------------------------ #
    def _available_tokens(self, chain_key: str) -> tuple[Token, ...]:
        """Los tokens que se pueden elegir en esta red: catálogo más añadidos.

        La regla —catálogo más lo guardado, sin repetir— vive en `widgets` porque la
        tarjeta de puentes la usa igual: un token añadido por su contrato tiene que
        aparecer en las dos.
        """
        return tokens_for_chain(self._container.token_store, chain_key)

    def _refresh_tokens(self) -> None:
        """Rearma las patas para la red elegida, conservando lo que se eligió."""
        entrega, recibe = self._legs()
        self._card.reload_tokens(entrega, recibe)
        self._update_legs_labels()

    def _on_chain_changed(self) -> None:
        """Cambiar de red rearma las patas **y** la pata de envío."""
        self._refresh_tokens()
        self._card.reload_send_tokens()
        self._update_recipients()

    def _on_token_picked(self, which: str) -> None:
        """Abre el selector de tokens para una pata, y aplica lo que devuelva.

        El modal puede devolver dos cosas distintas: un token ya elegido, o una
        **dirección** que hay que importar. La segunda no se resuelve dentro del
        diálogo a propósito —leer un contrato es una petición de red y el bucle
        anidado de `QDialog.exec()` no avanza las tareas de `qasync`—, así que
        vuelve aquí y se hace por el camino asíncrono de siempre.
        """
        cadena = self._chain.currentData()
        if not isinstance(cadena, str):
            return
        actual = {
            "base": self._base.currentData(),
            "quote": self._contra.currentData(),
        }.get(which)
        dialogo = TokenPickerDialog(
            self._container,
            cadena,
            self._balances,
            selected=actual if isinstance(actual, Token) else None,
            parent=self,
        )
        if dialogo.exec() != TokenPickerDialog.Accepted:
            direccion = dialogo.import_address()
            if direccion:
                spawn(self._do_lookup_token(cadena, direccion, select=which))
            return
        token = dialogo.chosen()
        if token is None:
            return
        combo = self._base if which == "base" else self._contra
        indice = position_of(combo, token)
        if indice >= 0:
            combo.setCurrentIndex(indice)
        elif which == "send":
            self._card.reload_send_tokens()

    async def _do_lookup_token(
        self, chain_key: str, address: str, *, select: str = ""
    ) -> None:
        """Lee el contrato de un token por su dirección y lo deja en la lista.

        El símbolo y los decimales se **leen** de la cadena y no se piden a mano: un
        decimal equivocado no da error, da un precio mil veces mayor o menor.
        """
        try:
            token = await self._container.token_lookup.by_address(chain_key, address)
        except Exception as error:
            self._card.set_status(f"No se pudo añadir el token: {error}")
            return

        # Ya estaba en el almacén —o en el catálogo—: se refresca igual, porque lo
        # que importa es que quede seleccionado y visible, no de dónde salió.
        #
        # Y se selecciona por **identidad**, no por el texto de la etiqueta: dos
        # tokens de la misma red pueden publicar el mismo símbolo —en Polygon lo
        # hacen los dos USDC— y ahí la etiqueta lleva la dirección pegada, así que
        # buscar por texto no encontraría el que se acaba de añadir.
        self._container.token_store.add(token)
        self._refresh_tokens()
        combo = self._base if select != "quote" else self._contra
        indice = position_of(combo, token)
        if indice >= 0:
            combo.setCurrentIndex(indice)
        # Y se avisa a quien tenga la lista de tokens vigilados: la cartera tiene la
        # suya, y un token que se añade aquí y no aparece allí sería la misma función
        # dando dos respuestas.
        self.token_added.emit(token)
        self._card.set_status(
            f"Añadido {token.symbol} ({token.decimals} decimales) desde "
            f"{token.address}. Ya se puede cotizar, y queda guardado para la "
            f"próxima vez."
        )

    def prepare_with_token(self, token: Token) -> None:
        """Deja el par listo para operar con un token, viniendo de la cartera.

        Es lo que hace que «intercambiar este token» signifique algo: pone la **red**
        del token, lo elige como lo que se entrega y deja la pata de enfrente en la
        stablecoin de esa red, que es la moneda con la que se compara todo lo demás.

        No cotiza sola. Cotizar es una llamada a la red por cada motor activo, y
        hacerlo al llegar —cuando el usuario todavía no ha escrito cuánto quiere
        entregar— sería gastar seis llamadas para contestar una pregunta que aún no
        se ha hecho.
        """
        indice_red = -1
        for indice in range(self._chain.count()):
            if self._chain.itemData(indice) == token.chain:
                indice_red = indice
                break
        if indice_red < 0:
            self._card.set_status(
                f"La red «{token.chain}» no está en la lista de la pestaña de swap."
            )
            return

        # Cambiar de red reconstruye las dos listas de tokens, así que va primero.
        if self._chain.currentIndex() != indice_red:
            self._chain.setCurrentIndex(indice_red)

        # Y si el token no estuviera en la lista —el caso de un token que se ve en la
        # cartera porque lo leyó un nodo, pero que nadie ha añadido—, se añade aquí,
        # que es lo que hace que el botón no pueda fallar en silencio.
        #
        # El nativo no necesita nada de esto: `tokens_for_chain` lo pone primero en
        # todas las redes, así que ETH y POL ya están en la lista antes de que nadie
        # los pida. Y no se guarda aunque se pida: un token sin dirección no tiene
        # contrato que pegar, y el almacén lo rechaza diciéndolo.
        if position_of(self._base, token) < 0:
            self._container.token_store.add(token)
            self._refresh_tokens()

        indice = position_of(self._base, token)
        if indice < 0:
            self._card.set_status(
                f"No se pudo poner «{token.symbol}» en la pata de entrega."
            )
            return
        self._base.setCurrentIndex(indice)

        # La otra pata, contra la stablecoin de la red. No se pisa si ya era la
        # stablecoin: `reload_tokens` la conserva entre redes cuando existe, y
        # volver a ponerla aquí no cambiaría nada.
        from amigocompora.engines.catalog import quote_token

        referencia = quote_token(token.chain)
        if referencia is not None and not referencia.is_same_asset(token):
            indice_contra = position_of(self._contra, referencia)
            if indice_contra >= 0:
                self._contra.setCurrentIndex(indice_contra)

        self._update_legs_labels()
        self._card.set_status(
            f"Listo para convertir {token.symbol} en {CHAINS[token.chain].name}. "
            f"Escribe el importe y pulsa «Cotizar rutas»."
        )

    def _update_legs_labels(self) -> None:
        """Qué se entrega y qué se recibe, y en qué unidad va la cantidad."""
        self._card.refresh_leg_labels()
        self._card.refresh_balances()
        self._schedule_quote()

    def _schedule_quote(self) -> None:
        """Programa una cotización tras una pausa corta, si el par tiene sentido.

        No cotiza si falta alguna pata o si las dos son el mismo activo: en ese caso
        no hay operación que cotizar, y pedir precios igualmente gastaría peticiones
        a los motores. Cada llamada reinicia la cuenta atrás, así que sólo el último
        cambio se convierte en petición.

        **Lo que había en pantalla se vacía aquí mismo**, en cuanto cambia algo:
        rutas, panel, oportunidades, la línea de la ruta elegida y el borrador
        preparado describían el importe o el par anteriores, y dejarlos mientras
        llega la cotización nueva era enseñar como actual algo que ya no lo es
        —o peor, ofrecer para firmar lo que ya no está en pantalla—. La
        cotización en vuelo se cancela con lo demás: una petición que ya no
        interesa no debe gastar cuota ni competir con la nueva.
        """
        entrega, recibe = self._legs()
        if entrega is None or recibe is None:
            self._quote_timer.stop()
            self._invalidate_quote()
            self._clear_results("Elige las dos patas del par para poder cotizar.")
            return
        if entrega.is_same_asset(recibe):
            self._quote_timer.stop()
            self._invalidate_quote()
            self._clear_results(
                f"«{entrega.symbol}» en las dos patas no es una operación: "
                f"elige dos tokens distintos."
            )
            return
        self._quote_timer.start()
        self._invalidate_quote()
        self._clear_results("Cotizando…")

    def _on_invert(self) -> None:
        """Da la vuelta al par, conservando lo elegido en cada pata.

        Se intercambian **posiciones** y no valores: lo que estaba en «recibes»
        pasa a «entregas» y al revés. Si alguna de las dos no existe en la otra
        lista —puede pasar con un token añadido que aún no está en la contraria— no
        se toca nada, porque un par a medio invertir es peor que uno sin invertir.
        """
        entrega = self._base.currentData()
        recibe = self._contra.currentData()
        if not isinstance(entrega, Token) or not isinstance(recibe, Token):
            return
        indice_recibe = position_of(self._base, recibe)
        indice_entrega = position_of(self._contra, entrega)
        if indice_recibe < 0 or indice_entrega < 0:
            return
        self._base.setCurrentIndex(indice_recibe)
        self._contra.setCurrentIndex(indice_entrega)
        self._update_legs_labels()

    def _on_max(self) -> None:
        """Pone el importe entero del saldo disponible en la pata que toque.

        Se delega en la tarjeta, que es quien sabe cuál de las patas está mirando
        el usuario y cuántos decimales admite su campo.
        """
        self._card.apply_max()

    # ------------------------------------------------------------------ #
    # Cotizar
    # ------------------------------------------------------------------ #
    def _on_quote(self) -> None:
        """Lanza una cotización nueva. La vieja, si sigue en vuelo, se cancela.

        No se desactiva el botón de cotizar: una cotización automática no debe impedir
        pedir otra cuando el usuario cambie algo mientras la anterior llega.
        """
        self._quote_timer.stop()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Sin bucle asíncrono —una prueba síncrona, nunca la aplicación, que corre
            # sobre qasync— no hay dónde lanzar la petición. Se sale sin ruido.
            return
        self._invalidate_quote()
        self._card.set_status("Cotizando…")
        self._quote_task = spawn(self._do_quote(self._quote_seq))

    def _invalidate_quote(self) -> None:
        """Da por vieja la cotización en vuelo y la detiene si sigue viva.

        Sube el número de secuencia —lo que hace que sus resultados se descarten
        aunque lleguen, ver `_do_quote`— y cancela la tarea: los motores se
        preguntaron para el importe anterior, y dejarlos correr gastaba cuota de
        las fuentes públicas y competía con la cotización nueva por el mismo
        hueco de red. Cancelar una tarea es seguro aquí: los casos de uso no
        dejan estado a medias, y el `finally` de `_do_quote` recalcula los
        botones desde lo que haya en pantalla, se cancele o no.
        """
        self._quote_seq += 1
        task = self._quote_task
        self._quote_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _do_quote(self, seq: int | None = None) -> None:
        # Una llamada directa (sin `seq`) es la cotización vigente. Una lanzada desde
        # `_on_quote` trae su número: si para cuando llega ya hay otra más nueva, sus
        # resultados se descartan en vez de pintar un par que el usuario ya cambió.
        if seq is None:
            self._quote_seq += 1
            seq = self._quote_seq
        try:
            entrega, recibe = self._legs()
            if entrega is None or recibe is None:
                self._card.set_status("Elige el token y la moneda contra la que cotizarlo.")
                self._clear_results("Elige las dos patas del par para poder cotizar.")
                return
            if entrega.is_same_asset(recibe):
                # Se dice en vez de impedirlo: los dos botones son libres a
                # propósito —bloquear combinaciones convierte un error evidente en un
                # control que se mueve solo y no se entiende—, y la etiqueta del par
                # ya lo está enseñando.
                texto = (
                    f"«{entrega.symbol}» en las dos patas no es una operación: "
                    f"elige dos tokens distintos."
                )
                self._card.set_status(texto)
                self._clear_results(texto)
                return

            # Qué lado es `base` **es** la dirección del par: `base` es lo que se
            # entrega. Y la cantidad se expresa siempre en el `base`, así que con el
            # par bien armado la dirección no vuelve a aparecer en ninguna cuenta de
            # las de después.
            pair = TradingPair(base=entrega, quote=recibe)
            amount = pair.base.amount(self._card.amount_value())
            comparison = await self._container.compare_prices(pair, amount)
            # Si mientras llegaba esta respuesta el usuario cambió el par o el importe,
            # ya hay otra cotización en camino: ésta describe algo que ya no está en
            # pantalla y no se pinta.
            if seq != self._quote_seq:
                return
            self._comparison = comparison
            self._fill_routes(comparison)
            # La mejor ruta queda elegida. Obligar a pulsar una fila después de haber
            # pedido la comparación es pedir dos veces lo mismo, y deja los botones
            # apagados sin que se vea por qué.
            if self._routes.count() > 0:
                self._routes.select_row(0)
            # También buscar oportunidades con el umbral por defecto, sobre la
            # comparación que ya está en pantalla: volver a pedirla a todos los
            # motores era una segunda ronda completa de la misma cotización, y el
            # mayor trozo del tiempo que se pasaba en «Cotizando…».
            opps = await self._container.scan_opportunities(
                pair, amount, comparison=comparison
            )
            if seq != self._quote_seq:
                return
            self._fill_opps(opps)
            self._card.set_status(_resumen_de(comparison))
        except Exception as error:
            self._card.set_status(f"Error: {error}")
            self._clear_results(f"No se pudo cotizar: {error}")
        finally:
            self._card.quote_btn.setEnabled(True)
            self._on_quote_selected()

    def _fill_routes(self, comp: PriceComparison) -> None:
        # Se despliega **antes** de rellenar, no después: la tarjeta es la que dice
        # que la cotización llegó, y plegada el botón «Cotizar» parecería no haber
        # hecho nada.
        self._routes_card.set_expanded(True)
        # Qué es firmable se pregunta **una vez** por ruta y se guarda: lo piden el
        # filtro del ajuste, la marca de la tarjeta y los botones, y tres llamadas
        # al registro para el mismo dato serían tres sitios donde puede cambiar de
        # respuesta a mitad de pintar.
        pares = [(quote, self._container.prepare_swap.can_build(quote)) for quote in comp.ranked]
        ocultas = 0
        if self._container.settings.ui.hide_quote_only_routes:
            # El ajuste pide una lista sin lo que no se puede firmar: se van
            # enteras las rutas de motores que no construyen el swap —no se
            # pueden firmar— en vez de quedarse marcadas en ámbar. Se cuentan
            # para poder decir cuántas se ocultaron: una lista con menos filas
            # de las que hay no puede parecer completa.
            pares = [(quote, firmable) for quote, firmable in pares if firmable]
            ocultas = len(comp.quotes) - len(pares)
        quotes = tuple(quote for quote, _ in pares)
        self._shown_quotes = quotes
        simbolo = comp.pair.base.symbol
        if ocultas:
            self._route_count.setText(
                f"{len(quotes)} de {len(comp.quotes)} ruta(s) de {simbolo} · "
                f"{ocultas} de sólo cotización oculta(s)"
            )
        else:
            self._route_count.setText(f"{len(comp.quotes)} ruta(s) de {simbolo}")
        self._routes.set_quotes(quotes, firmables=tuple(f for _, f in pares))
        self._apply_decimales()
        # El panel de detalle sólo tiene sentido cuando hay rutas que elegir: sin
        # ninguna, el rótulo que ocupa el sitio de la lista ya dice qué hacer y el
        # panel repetiría la instrucción debajo.
        self._detail.setVisible(bool(quotes))
        if not quotes and ocultas:
            # La lista no está vacía porque nadie contestara: está vacía porque
            # todo lo que contestó sólo cotiza y el ajuste lo oculta. Decir
            # «ningún motor devolvió nada» sería falso, así que se dice la
            # verdad y se nombra la salida —la clave de la configuración—, que
            # es lo único que el usuario puede hacer al respecto.
            motivo = (
                f"Las {ocultas} ruta(s) disponibles sólo cotizan y no construyen el "
                f"swap, así que este ajuste no las enseña. Sirven para comparar "
                f"cifras, no para firmar; para verlas, pon hide_quote_only_routes = "
                f"false en la sección [ui] de la configuración."
            )
        else:
            motivo = (
                "Ningún motor activo devolvió una ruta para este par y este importe. "
                "Cotizar no firma nada: se puede probar con otro importe o con otro par."
            )
        set_empty(self._routes, self._routes_empty, motivo)
        self._routes.cap_height()

    def _apply_decimales(self) -> None:
        """Pone los importes de la lista y del panel a 4 decimales, o completos."""
        completos = self._todos_decimales.isChecked()
        for card in self._routes.cards():
            card.set_completo(completos)
        self._detail.set_completo(completos)

    def _fill_opps(self, opps: tuple[Opportunity, ...]) -> None:
        self._opps_card.set_expanded(True)
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
        set_empty(self._opp_table, self._opps_empty, self._motivo_sin_oportunidades())
        _cap_height(self._opp_table)

    def _motivo_sin_oportunidades(self) -> str:
        """Por qué la tabla está vacía, con el número que lo hace creíble.

        Una tabla vacía no distingue «no hay nada» de «está roto», y esta se quedó
        con la sospecha: el umbral es de 10 bps —una décima de punto, que es donde
        las comisiones de los dos lados dejan de comerse el hallazgo— y el mejor
        diferencial medido en vivo fue de 6 bps. Es decir: la pantalla tenía razón y
        no lo decía.

        Así que dice tres cosas: cuál es el umbral y de dónde sale, **el mejor
        diferencial que sí se vio** aunque no llegue, y qué se puede hacer al
        respecto. El umbral no se toca —es una decisión del motor, no de la vista—;
        lo que se arregla es el silencio.
        """
        umbral = DEFAULT_MIN_NET_BPS
        if self._comparison is None:
            return (
                f"Todavía no has cotizado. El umbral es de {umbral} bps —una décima "
                f"de punto— porque por debajo las comisiones de los dos lados se "
                f"comen el hallazgo."
            )
        # Se vuelve a derivar sin umbral sobre la comparación que ya está en
        # pantalla: no cuesta red ninguna y es el mismo cálculo, así que el número
        # que se enseña sale de las cotizaciones que el usuario acaba de ver.
        vistas = self._container.scan_opportunities.from_comparison(
            self._comparison, min_net_bps=BasisPoints(0)
        )
        mejor = vistas[0] if vistas else None
        if mejor is None:
            return (
                f"Ningún par de venues deja un diferencial neto positivo: con las "
                f"comisiones de los dos lados, cruzar el dinero de uno a otro cuesta "
                f"más de lo que separa sus precios. El umbral es de {umbral} bps."
            )
        return (
            f"El mejor diferencial visto ahora es de {mejor.net_spread_bps} bps "
            f"—{mejor.best.venue.name} contra {mejor.reference.venue.name}—, por "
            f"debajo del umbral de {umbral} bps. Por debajo de esa décima de punto "
            f"las comisiones de los dos lados se comen el hallazgo, así que no se "
            f"reporta. Con un importe mayor el impacto pesa más y el diferencial "
            f"suele estrecharse, no ensancharse."
        )

    def _clear_results(self, motivo: str) -> None:
        """Deja la lista y la tabla vacías y **diciendo por qué** están vacías."""
        self._routes.set_quotes((), firmables=())
        self._opp_table.setRowCount(0)
        self._shown_quotes = ()
        self._route_count.setText("")
        # El panel de detalle se va con la lista: sin rutas que elegir, el rótulo
        # que ocupa su sitio ya dice qué hacer y el panel sobraría.
        self._detail.clear()
        self._detail.setVisible(False)
        # La ruta cuyo coste estaba estimado era de lo anterior —otro importe,
        # otro par—: se olvida con lo demás para que el panel no conserve una
        # cifra que ya no describe nada. Las transacciones de la lista de pasos
        # **no** se tocan: son hechos que ocurrieron, y cambiar el importe no los
        # deshace; se limpian con «Empezar de nuevo».
        self._cost_quote = None
        set_empty(self._routes, self._routes_empty, motivo)
        set_empty(self._opp_table, self._opps_empty, motivo)
        self._routes.cap_height()
        _cap_height(self._opp_table)
        # Y botones y línea de la ruta elegida se recalculan aquí mismo: con la
        # lista vacía no hay nada que preparar ni firmar, y dejarlos encendidos
        # apuntando a una ruta recién retirada es ofrecer un botón que no lleva
        # a ninguna parte. Pasa en cada cambio de importe o de par, así que tiene
        # que ser barato: `_on_quote_selected` sólo lee estado y pinta etiquetas.
        self._on_quote_selected()

    # ------------------------------------------------------------------ #
    # La ruta elegida
    # ------------------------------------------------------------------ #
    def _selected_quote(self) -> Quote | None:
        if self._comparison is None:
            return None
        # El índice es el de la fila visible, y la lista puede estar enseñando
        # un subconjunto —las de sólo cotización ocultas—: por eso la fila
        # apunta a lo que se enseña y no a la comparación entera, donde los
        # índices no coincidirían.
        index = self._routes.current_row()
        mostradas = self._shown_quotes
        return mostradas[index] if 0 <= index < len(mostradas) else None

    def _on_quote_selected(self) -> None:
        quote = self._selected_quote()
        firmable = quote is not None and self._container.prepare_swap.can_build(quote)
        motivos = self._execution_blockers(quote.pair if quote is not None else None, quote)

        # El desenlace que el botón enseña vale para **su** ruta: al elegir otra,
        # lo que hubo que contar se retira aquí. Mientras corre una operación no
        # se toca —el texto lo lleva la narración—, y al volver de `_do_execute`
        # la ruta es la misma, así que el desenlace recién escrito se conserva.
        if quote is not self._executed_quote and not self._exec_running:
            self._exec_done = False
            self._exec_btn.setText(_EXEC_LABEL)

        self._exec_btn.setEnabled(
            quote is not None and not motivos and not self._exec_running and not self._exec_done
        )

        if quote is None:
            # Con la lista vacía el rótulo que ocupa su sitio ya dice qué hacer;
            # repetir aquí lo mismo sería la misma frase dos veces en la misma
            # tarjeta. La línea existe para cuando **sí** hay rutas y ninguna
            # elegida, que es cuando hace falta decir qué falta.
            self._chosen.setText(
                "Selecciona una ruta para ejecutarla."
                if self._routes.count() > 0
                else ""
            )
            self._chosen.setToolTip("")
            self._card.set_estimate("—", known=False)
            self._detail.clear()
        else:
            resumen = (
                f"{quote.venue.name} · motor {quote.engine_id} · "
                f"entregas {quote.amount_in} y recibes {quote.amount_out} "
                f"({quote.pair.base.symbol} → {quote.pair.quote.symbol})"
            )
            self._chosen.setText(f"<b>Ruta elegida:</b> {resumen}")
            # La línea no se envuelve, así que en una ventana estrecha se recorta.
            # Aquí está la misma frase entera, para que recortar no sea perder.
            self._chosen.setToolTip(f"Ruta elegida: {resumen}")
            self._card.set_estimate(f"≈ {quote.amount_out}")
            self._detail.set_quote(
                quote,
                firmable=firmable,
                owner=self._owner(),
                # El valor **vivo**, no el de la configuración cargada al
                # arrancar: el engranaje lo cambia sin reiniciar, y esta fila
                # tiene que decir la tolerancia que se aplicará de verdad.
                slippage_bps=self._container.slippage.bps,
            )
            # Al cambiar de ruta el coste vuelve a «se estima al ejecutar»: la
            # cifra que hubiera era de otra ruta. La excepción es la ruta cuyo
            # coste se acaba de estimar, que es el caso que se da al volver de
            # `_do_execute` con el paso ESTIMATE recién pintado.
            if quote is not self._cost_quote:
                self._detail.reset_cost()

        # El motivo se enseña sólo cuando ya hay una cotización elegida: antes de eso
        # el botón apagado no dice nada que el usuario no sepa ya, y un aviso
        # permanente en rojo se aprende a ignorar.
        self._exec_note.setText(
            ""
            if quote is None or not motivos
            else "No se puede ejecutar: " + " · ".join(motivos) + "."
        )
        self._refresh_upgrade_offer(motivos)
        self._refresh_mode_chip()

    def _refresh_upgrade_offer(self, motivos: tuple[str, ...]) -> None:
        """Ofrece el cambio de modo sólo cuando el modo es lo que bloquea.

        Se enseña el atajo cuando el modo está entre los motivos y no en cualquier
        caso: si además falta el interruptor o la cartera, cambiar de modo no
        desbloquearía nada, y un botón que no arregla lo que promete es peor que no
        tenerlo.
        """
        modo_bloquea = any("no permite emitir" in motivo for motivo in motivos)
        destino = self._container.guard.upgrade_path(Capability.BROADCAST_TX)
        if not modo_bloquea or destino is None:
            self._upgrade_btn.setVisible(False)
            return
        self._upgrade_btn.setText(f"Cambiar a {destino.label} →")
        self._upgrade_btn.setToolTip(
            f"Otorga las capacidades de {destino.label}: {destino.description} "
            "Es un cambio de modo, y el modo sólo lo cambia una acción tuya."
        )
        self._upgrade_btn.setVisible(True)

    def _refresh_mode_chip(self) -> None:
        mode = self._container.guard.mode
        self._card.set_mode(
            mode.label,
            can_sign=self._container.guard.allows(Capability.BROADCAST_TX),
        )

    def _on_upgrade_mode(self) -> None:
        """Sube al modo que concede firmar y emitir, por acción explícita.

        `ModeGuard.set_mode` está documentado como algo que **sólo** puede venir de
        una acción del usuario: ni un caso de uso ni un motor pueden subir de modo
        para desbloquearse a sí mismos. Un botón que alguien pulsa es exactamente esa
        acción; lo que no puede haber es que lo haga el programa.
        """
        destino = self._container.guard.upgrade_path(Capability.BROADCAST_TX)
        if destino is None:
            return
        self._container.guard.set_mode(destino)
        self._card.set_status(
            f"Modo cambiado a {destino.label}. La ejecución sigue dependiendo de los "
            "topes y del interruptor de `config.toml`."
        )
        self._on_quote_selected()

    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo, de la cartera y de la red.

        Público porque quien sabe que el modo ha cambiado es la ventana, no esta
        pestaña: `ModeGuard.subscribe` avisa a quien se apunte, y la pestaña no se
        apunta sola. Existe en vez de que la ventana llame a `_on_quote_selected`
        porque un método privado de otra clase no es un atajo, es una interfaz que
        nadie declaró.

        Repinta **también la tarjeta de puentes** por el mismo motivo —su botón de
        firmar depende del mismo modo— y la tarjeta de arriba, porque la ventana
        avisa por aquí de dos cosas distintas: que cambió el modo y que se guardó o
        se borró una credencial. Con la clave recién puesta, la dirección que firma
        y los saldos son justo lo que hay que ir a buscar.
        """
        self._card.set_owner(self._owner() or None)
        self._update_recipients()
        self._on_quote_selected()
        self._bridges.refresh_execution_state()
        self._card.refresh_balances()

    def _update_recipients(self) -> None:
        """Las direcciones que se ofrecen para el destino de la retirada."""
        cadena = self._chain.currentData()
        if isinstance(cadena, str) and cadena:
            self._card.set_recipients(self._recipient_candidates(cadena))

    # ------------------------------------------------------------------ #
    # Lo que falta para firmar
    # ------------------------------------------------------------------ #
    def _execution_blockers(
        self,
        pair: TradingPair | None = None,
        quote: Quote | None = None,
    ) -> tuple[str, ...]:
        """Todo lo que falta para poder firmar y emitir. Vacío significa que sí.

        Se delega en `ui/execution_gate.py`, que llama a las **mismas**
        comprobaciones que correrán al firmar. La página no reescribe esas reglas:
        tener dos versiones de la misma regla es tener una que se desvía, y la que
        se desvía es siempre la que no se ejecuta.
        """
        return execution_blockers(self._container, pair=pair, quote=quote)

    def _cap_blockers(self, quote: Quote) -> tuple[str, ...]:
        """Lo que los topes dirían de esta operación. Delegado, no reescrito."""
        return cap_blockers(self._container, quote)

    def _recipient_candidates(self, chain_key: str) -> list[str]:
        """Las direcciones conocidas y válidas en esa red, la propia primero."""
        return recipient_candidates(self._container, chain_key)

    # ------------------------------------------------------------------ #
    # Ejecutar: un solo botón, y lo que va pasando
    # ------------------------------------------------------------------ #
    def _on_execute(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._card.set_status("Selecciona primero una ruta de la tabla.")
            return
        # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el modo se
        # puede cambiar desde la barra mientras esta pestaña está abierta, y un botón
        # sólo se repinta cuando algo lo repinta. La puerta de verdad está en
        # `ExecuteSwap`; esto es para no llegar hasta ella y volver con un error que
        # se puede decir antes de molestar a nadie.
        motivos = self._execution_blockers(quote.pair, quote)
        if motivos:
            self._card.set_status("No se puede ejecutar: " + " · ".join(motivos) + ".")
            return
        destinatario = self._owner()
        if not destinatario:
            self._card.set_status("No hay cartera configurada, así que no hay con qué firmar.")
            return
        self._exec_running = True
        self._exec_btn.setEnabled(False)
        self._exec_btn.setText("Preparando el swap…")
        self._card.set_status(
            "Operación en marcha: cada transacción pedirá su propia confirmación."
        )
        spawn(self._do_execute(quote, destinatario))

    def _on_step(self, update: StepUpdate, quote: Quote) -> None:
        """Lo que el caso de uso cuenta mientras ejecuta: a la lista y al botón.

        Corre en el hilo de la interfaz —`ExecuteSwap` avisa desde la misma
        tarea— y no decide nada: pinta. El texto del botón sigue al último paso
        que empezó, que es lo que da el avance en vivo sin abrir nada.
        """
        self._steps.record(update)
        self._new_btn.setVisible(True)
        if update.state is StepState.RUNNING:
            self._exec_btn.setText(update.title)
        # La fila «Coste de red» del panel de detalle se rellena aquí: la
        # estimación vive ahora en el paso ESTIMATE, y lo que enseña es lo mismo
        # que quedó en la lista —la cifra, el motivo o la raya de «no aplica»—.
        if update.step is SwapStep.ESTIMATE and update.state is StepState.DONE:
            self._cost_quote = quote
            self._detail.set_cost(update.cost, update.cost_error)

    async def _do_execute(self, quote: Quote, recipient: str) -> None:
        def narrar(update: StepUpdate) -> None:
            self._on_step(update, quote)

        # El panel arranca con la operación: vacía lo de la anterior —si la hubo—
        # y fija la red, que es lo que hace falta para enlazar cada hash con su
        # explorador.
        self._steps.start(quote.pair.base.chain)
        try:
            receipt = await self._container.execute_swap(
                quote, recipient=recipient, on_step=narrar
            )
            # La línea de estado sigue, pero la confirmación real es la ventana: la
            # línea queda debajo de los importes y no se lee tras emitir un dinero.
            self._exec_done = True
            self._executed_quote = quote
            self._exec_btn.setText(_DONE_LABEL)
            self._card.set_status(
                f"Emitida {receipt.tx_hash} · estado {receipt.status.value}."
            )
            self._mostrar_recibo(quote, receipt, recipient)
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._exec_btn.setText(_EXEC_LABEL)
            self._card.set_status("Cancelaste la operación. No se firmó ni se emitió nada.")
        except Exception as error:
            self._exec_btn.setText(_EXEC_LABEL)
            self._card.set_status(f"Error: {error}")
        finally:
            self._exec_running = False
            self._on_quote_selected()

    def _on_start_over(self) -> None:
        """Vuelve al estado inicial: lista limpia y botón como recién llegado.

        No toca la cotización ni la ruta elegida —el usuario sigue donde estaba—
        y no firma nada: lo único que retira es el relato de la operación, que ya
        cumplió su función.
        """
        self._steps.clear()
        self._new_btn.setVisible(False)
        self._exec_done = False
        self._executed_quote = None
        self._cost_quote = None
        self._exec_btn.setText(_EXEC_LABEL)
        self._card.set_status("")
        self._on_quote_selected()

    # ------------------------------------------------------------------ #
    # El deslizamiento por defecto
    # ------------------------------------------------------------------ #
    def _on_slippage(self) -> None:
        """Configura la tolerancia con la que se construyen los swaps.

        El valor se aplica **en el acto** —vive en `DefaultSlippage`, que es lo
        que lee el constructor del swap— y se guarda en `config.toml` para que
        siga valiendo tras cerrar. Si la escritura falla, se dice: la tolerancia
        rige esta sesión y eso es cierto, pero callar que no se guardó dejaría al
        usuario creyendo que sí.
        """
        dialogo = SlippageDialog(self._container.slippage.bps, parent=self)
        if dialogo.exec() != SlippageDialog.Accepted:
            return
        bps = dialogo.value_bps()
        self._container.slippage.set(bps)
        self._card.set_slippage_bps(bps)
        porcentaje = BasisPoints(bps).as_percent()
        aviso = ""
        try:
            set_execution_slippage_bps(config_file(), bps)
        except Exception as error:
            aviso = (
                f" No se pudo guardar en config.toml ({error}): la tolerancia vale "
                f"para esta sesión."
            )
        self._card.set_status(
            f"Deslizamiento por defecto: {porcentaje:f} %. Se aplica al construir "
            f"los swaps de esta sesión.{aviso}"
        )
        # La fila «Deslizamiento máx.» del panel de detalle enseña el valor
        # vigente, así que se repinta con el nuevo.
        self._on_quote_selected()

    # ------------------------------------------------------------------ #
    # Enviar: sacar fondos de la cartera
    # ------------------------------------------------------------------ #
    def _on_send(self) -> None:
        """Recoge el formulario y llama al caso de uso. Sin atajos.

        El formulario no comprueba nada: devuelve lo que se escribió y el caso de uso
        hace el resto en su orden —modo, red, destino, topes, saldo, permiso, firma—.
        Duplicar aquí una parte de ese orden sería tener dos versiones de la misma
        regla, y la que se desvía es siempre la que no se ejecuta.
        """
        token = self._card.selected_send_token()
        if token is None:
            self._card.set_send_status("Elige el token que quieres enviar.")
            return
        destinatario = self._card.recipient()
        if not destinatario:
            self._card.set_send_status("Falta la dirección de destino.")
            return
        try:
            amount = token.amount(self._card.send_amount())
        except Exception as error:
            self._card.set_send_status(f"Importe no válido: {error}")
            return
        self._card.send_btn.setEnabled(False)
        self._card.set_send_status(f"Enviando {amount} de {token.symbol}…")
        spawn(self._do_send(token, amount, destinatario))

    async def _do_send(self, token: Token, amount, recipient: str) -> None:  # type: ignore[no-untyped-def]
        try:
            receipt = await self._container.withdraw_funds(token, amount, recipient=recipient)
            self._card.set_send_status(
                f"Retirada emitida: {amount} {token.symbol} → {recipient[:10]}… "
                f"Hash {receipt.tx_hash}."
            )
        except Exception as error:
            # El motivo va entero a la pantalla. Un caso de uso que explica por qué
            # no firma —«no se pudo valorar», «la credencial cambió», «no cabe el
            # gas»— pierde todo su valor si la interfaz lo resume en «error».
            self._card.set_send_status(f"No se envió nada: {error}")
        finally:
            self._card.send_btn.setEnabled(True)
            self._card.refresh_balances()

    # ------------------------------------------------------------------ #
    # La confirmación de la operación
    # ------------------------------------------------------------------ #
    def _mostrar_recibo(self, quote: Quote, receipt: BroadcastReceipt, recipient: str) -> None:
        """Abre la confirmación de la operación emitida, sin bloquear la pantalla.

        Se guarda la referencia en la página: una ventana sin dueño la recoge el
        recolector de basura en cuanto sale esta función, y el usuario no llega a
        verla. Se abre sin `exec()` porque un diálogo modal aquí anidaría el bucle
        de eventos dentro de la tarea de la operación.
        """
        ventana = SwapReceiptDialog(
            quote=quote, receipt=receipt, recipient=recipient, parent=self
        )
        self._recibo = ventana
        ventana.show()
        ventana.raise_()
        ventana.activateWindow()


def _resumen_de(comparison: PriceComparison) -> str:
    """Lo que se dice bajo el botón después de cotizar, con la procedencia.

    Se dice cuando falta una fuente —«sin respuesta: geckoterminal»— porque una
    tabla a la que le falta un motor se lee como «esto es todo lo que hay», y puede
    que la mejor ruta fuera justo la del que no contestó.
    """
    note = "con estimaciones" if comparison.has_estimates else "todo medido"
    if comparison.has_unknown_fees:
        note += " · hay comisiones no desglosadas"
    if comparison.has_partial_sources:
        note += f" · sin respuesta: {', '.join(comparison.failed_engines)}"
    return (
        f"{len(comparison.quotes)} venue(s) · spread {comparison.spread_bps} · {note} "
        f"· observado {comparison.observed_at.isoformat()}"
    )


def _hugs_content(card: Card) -> Card:
    """Una tarjeta que mide lo que mide su contenido, y ni un píxel más.

    Con la política por omisión una tarjeta *puede* crecer, y crecía: la de rutas
    acababa con ochenta píxeles de vacío entre los botones y su propio borde
    inferior, porque `QBoxLayout` reparte el sobrante entre todo lo que admita crecer
    aunque su contenido no llegue a llenarlo. `Maximum` no basta —el dato que usa el
    reparto es el peso, no la política—, así que se fija: el alto es el de su
    contenido, y el hueco que sobra se queda al final de la página, que es donde no
    molesta a nadie.
    """
    card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return card


def _cap_height(table: QTableWidget, filas: int = 10) -> None:
    """Le pone techo al alto de una tabla: el de sus filas, no el que le sobre.

    Una tabla con dos rutas dentro de una tarjeta que estira ocupaba trescientos
    píxeles de gris vacío debajo de la última fila. Con el techo puesto, la tarjeta
    mide lo que mide su contenido y el espacio libre se lo queda la sección de abajo,
    que es otra tabla y lo aprovecha cuando tiene filas.

    Es un **máximo**, no un alto fijo: por debajo sigue encogiendo sola, que es lo
    que hace falta cuando la ventana es pequeña o cuando no hay filas. El tope de
    filas evita que veinte rutas empujen los botones fuera de la pantalla; a partir
    de ahí se desplaza, que es lo que una tabla sabe hacer.
    """
    visible = min(table.rowCount(), filas)
    # Las filas de estas tablas son de una línea (`setWordWrap(False)` al
    # construirlas): la nota es una frase larga y, envuelta, cada fila pasaba de
    # treinta píxeles a ciento cincuenta y la tabla dejaba de leerse de un vistazo.
    alto = table.horizontalHeader().height() + 2 * table.frameWidth()
    for row in range(visible):
        alto += table.rowHeight(row)
    table.setMaximumHeight(alto if visible else 0)
