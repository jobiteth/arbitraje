"""Sección **Entre redes**: cruzar de una red a otra, y firmarlo si se puede.

Vive en su propio módulo y no dentro de `prices.py` porque es una operación
distinta, con su propio estado y sus propias comprobaciones —dos redes en vez de
una, un contrato de destino en vez de un router, una duración que puede ser de
minutos— y `prices.py` ya tiene tres tarjetas. Aun así la sección se **coloca**
dentro de la pestaña de swap: se elige igual —entregas, recibes, cuánto— y el
usuario que viene a cambiar una moneda por otra no tiene que aprender que cuando
las dos están en redes distintas la operación vive en otro sitio.

### Por qué se pide a todos los motores y se ordena

Un puente no tiene «el precio de mercado»: lo que se lleva el cruce y lo que
tarda son propiedades de cada proveedor, no de un pool. Así que la comparación no
es un extra, es todo lo que hay, y la tabla enseña las rutas **de mejor a peor**
por lo que entregan, con la comisión, la duración y de quién es cada cifra. El
orden lo pone `rank_bridges` en el caso de uso, no esta pantalla: si se ordenara
aquí, la ruta que se enseña como mejor podría no ser la que se ejecuta.

### Lo que se dice antes de firmar

Los mismos bloqueos escritos que el botón de swaps, más uno que aquí es nuevo y
que es el que más se va a encontrar quien pruebe esto: **la red de destino también
tiene que estar en `allowed_chains`**. Un puente sale de una red y el dinero
aparece en otra, así que una lista blanca que sólo cubriera la de origen dejaría
cruzar hacia una red que el usuario no autorizó. El texto del motivo es el de la
comprobación real —`ExecutionLimits.check_chain`— para que la pantalla y el
veredicto de al lado no puedan decir cosas distintas.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
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
from amigocompora.app.usecases.execute_bridge import bridge_notional
from amigocompora.domain.addresses import is_evm_address
from amigocompora.domain.chains import CHAINS, AddressFormat, chain
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionLimitExceededError,
    NoActiveEngineError,
)
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    Token,
)
from amigocompora.domain.modes import Capability
from amigocompora.engines.catalog import quote_token
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_SUCCESS, COLOR_WARNING
from amigocompora.ui.widgets import (
    Card,
    Field,
    set_empty,
    spawn,
    token_labels,
    tokens_for_chain,
)

#: La red de la que sale el dinero y la que lo recibe al abrir. Base y Polygon
#: porque es el camino que se pidió y el que tiene stablecoin barata en las dos
#: puntas; cambiarlas es un desplegable.
_ORIGEN_POR_DEFECTO = "base"
_DESTINO_POR_DEFECTO = "polygon"

#: Lo que dice la tabla cuando aún no se ha pedido nada. Igual que en la de swaps:
#: el mismo texto para el estado inicial y para el que queda tras un error, porque
#: los dos son «no hay rutas» y decir cosas distintas en los dos sitios es la forma
#: de que uno de los dos acabe mintiendo.
_SIN_RUTAS = (
    "Todavía no has buscado rutas. Elige las redes y los dos tokens, y pulsa "
    "«Buscar rutas»: buscar no firma ni emite nada."
)


class BridgesSection(QWidget):
    """La tarjeta «Entre redes» dentro de la pestaña de swap."""

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: BridgeComparison | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build())

        self._origin_chain.currentIndexChanged.connect(self._refresh_tokens)
        self._destination_chain.currentIndexChanged.connect(self._refresh_tokens)
        self._refresh_tokens()
        self._on_route_selected()

    # ------------------------------------------------------------------ #
    # Construcción
    # ------------------------------------------------------------------ #
    def _build(self) -> QWidget:
        card = Card(
            "Entre redes",
            subtitle="— cruzar de una red a otra; las rutas se piden a todos los motores activos",
        )

        top = QHBoxLayout()
        top.setSpacing(10)
        self._origin_chain = self._chain_combo(_ORIGEN_POR_DEFECTO)
        self._origin_token = QComboBox()
        self._amount = QDoubleSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        self._amount.setMinimumWidth(140)
        self._unit = QLabel("")
        self._unit.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 700;")

        origen = Field("ENTREGAS EN")
        origen.add(self._origin_chain)
        origen.add(self._origin_token, 1)
        origen.add(self._amount)
        origen.add(self._unit)

        self._destination_chain = self._chain_combo(_DESTINO_POR_DEFECTO)
        self._destination_token = QComboBox()
        self._out_estimate = QLabel("—")
        self._out_estimate.setObjectName("bigNumber")
        self._out_estimate.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._out_estimate.setStyleSheet(f"color: {COLOR_MUTED};")
        self._out_estimate.setMinimumWidth(140)
        self._out_estimate.setToolTip("Lo que devolvería la mejor ruta. Se rellena al buscar.")

        destino = Field("RECIBES EN")
        destino.add(self._destination_chain)
        destino.add(self._destination_token, 1)
        destino.add(self._out_estimate)

        top.addWidget(origen, 1)
        top.addWidget(destino, 1)
        card.add_row(top)

        bottom = QHBoxLayout()
        self._search_btn = QPushButton("Buscar rutas")
        self._search_btn.setToolTip(
            "Pregunta a todos los motores de puentes activos y enseña las rutas de "
            "mejor a peor. No firma ni emite nada."
        )
        self._search_btn.clicked.connect(self._on_search)
        bottom.addWidget(self._search_btn)
        self._pair = QLabel("—")
        self._pair.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 600;")
        bottom.addWidget(self._pair)
        bottom.addStretch()
        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        bottom.addWidget(self._status, 2)
        card.add_row(bottom)

        # La tabla y su rótulo, igual que en la de swaps: una tabla sin filas es un
        # rectángulo gris que no distingue «no has pedido nada» de «pediste y no
        # hay ruta».
        self._empty = QLabel("")
        self._empty.setObjectName("empty")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignCenter)
        card.body().addWidget(self._empty, stretch=1)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            [
                "Ruta",
                "Proveedor",
                "Motor",
                "Recibes",
                "Mínimo garantizado",
                "Comisión",
                "Duración",
                "Nota",
            ]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.itemSelectionChanged.connect(self._on_route_selected)
        card.body().addWidget(self._table, stretch=1)

        # La ruta elegida, escrita, entre la tabla y los botones: es lo que los
        # botones van a usar, y sin esta línea hay que deducir qué fila está
        # resaltada en una tabla de cifras.
        self._chosen = QLabel("Selecciona una ruta para prepararla o firmarla.")
        self._chosen.setObjectName("hint")
        self._chosen.setWordWrap(False)
        card.body().addWidget(self._chosen)

        actions = QHBoxLayout()
        self._prepare_btn = QPushButton("Preparar puente…")
        self._prepare_btn.setObjectName("secondary")
        self._prepare_btn.setToolTip(
            "Construye la transacción sin firmar de la ruta elegida. Este botón no "
            "firma ni emite nada."
        )
        self._prepare_btn.clicked.connect(self._on_prepare)
        self._prepare_btn.setEnabled(False)
        actions.addWidget(self._prepare_btn)

        self._exec_btn = QPushButton("Ejecutar: firmar y emitir…")
        self._exec_btn.setObjectName("danger")
        self._exec_btn.setToolTip(
            "Firma el cruce y lo emite a la red de origen. Es irreversible: el "
            "dinero sale de esa red y el tiempo del cruce no lo controla la "
            "aplicación."
        )
        self._exec_btn.clicked.connect(self._on_execute)
        self._exec_btn.setEnabled(False)
        actions.addWidget(self._exec_btn)
        actions.addStretch()
        card.add_row(actions)

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
        card.add_row(note_row)

        card.setSizePolicy(card.sizePolicy().horizontalPolicy(), card.sizePolicy().verticalPolicy())
        return card

    @staticmethod
    def _chain_combo(seleccionada: str) -> QComboBox:
        combo = QComboBox()
        for key in sorted(CHAINS):
            combo.addItem(f"{CHAINS[key].name} ({key})", key)
        index = combo.findData(seleccionada)
        if index >= 0:
            combo.setCurrentIndex(index)
        return combo

    # ------------------------------------------------------------------ #
    # Los desplegables de token
    # ------------------------------------------------------------------ #
    def _refresh_tokens(self) -> None:
        """Rearma las dos listas de tokens para las redes elegidas.

        Se conserva el token elegido cuando sigue existiendo en la red nueva —y se
        vuelve a la stablecoin de referencia cuando no—, porque cambiar de red con
        un token que ya no está deja el desplegable en un índice que no significa
        nada.
        """
        self._reload(self._origin_token, self._chain_key(self._origin_chain))
        self._reload(self._destination_token, self._chain_key(self._destination_chain))
        self._comparison = None
        self._clear_table(_SIN_RUTAS)
        self._on_route_selected()

    def _reload(self, combo: QComboBox, chain_key: str) -> None:
        previous = combo.currentData()
        tokens = tokens_for_chain(self._container.token_store, chain_key)
        combo.blockSignals(True)
        combo.clear()
        for label, token in zip(token_labels(tokens), tokens, strict=True):
            combo.addItem(label, token)
        index = 0
        if isinstance(previous, Token):
            for position, token in enumerate(tokens):
                if token.is_same_asset(previous):
                    index = position
                    break
            else:
                reference = quote_token(chain_key)
                index = _position_of(tokens, reference)
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)
        if combo is self._origin_token:
            token = combo.currentData()
            self._unit.setText(token.symbol if isinstance(token, Token) else "")

    def _chain_key(self, combo: QComboBox) -> str:
        return str(combo.currentData() or "")

    def _tokens(self) -> tuple[Token | None, Token | None]:
        origin = self._origin_token.currentData()
        destination = self._destination_token.currentData()
        return (
            origin if isinstance(origin, Token) else None,
            destination if isinstance(destination, Token) else None,
        )

    # ------------------------------------------------------------------ #
    # Buscar rutas
    # ------------------------------------------------------------------ #
    def _on_search(self) -> None:
        self._search_btn.setEnabled(False)
        self._status.setText("Preguntando a los motores de puentes…")
        spawn(self._do_search())

    def _request(self) -> tuple[BridgeRequest | None, str]:
        """La solicitud que describen los controles, o el motivo por el que no hay.

        La construye esta pantalla y no el caso de uso: la comparación que se
        ejecuta tiene que ser **la misma petición** que el usuario escribió, no dos
        construcciones parecidas que podrían diferir en un decimal.
        """
        origin, destination = self._tokens()
        if origin is None or destination is None:
            return None, "Elige el token en las dos redes."
        if origin.chain == destination.chain:
            return None, (
                f"Las dos patas están en {origin.chain}: eso es un swap, no un "
                f"puente. Cambia una de las dos redes —o usa la tarjeta de arriba—."
            )
        if origin.is_same_asset(destination) and origin.address == destination.address:
            # Mismo contrato en la misma red ya lo cazó la comprobación anterior;
            # esto caza el caso que sí pasa el filtro y no tiene sentido: el mismo
            # token consigo mismo a través de un puente que no cruza nada. Se deja
            # pasar el de dos redes, que es justo lo que se viene a hacer.
            return None, f"«{origin.symbol}» contra sí mismo no es una operación."
        request = BridgeRequest(
            origin=origin,
            destination=destination,
            amount_in=origin.amount(Decimal(str(self._amount.value()))),
        )
        return request, ""

    async def _do_search(self) -> None:
        try:
            request, motivo = self._request()
            if request is None:
                self._status.setText(motivo)
                self._clear_table(motivo)
                return
            comparison = await self._container.compare_bridges(request)
            self._comparison = comparison
            self._fill_table(comparison)
            if self._table.rowCount() > 0:
                self._table.selectRow(0)
            self._status.setText(_summary(comparison))
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._clear_table(f"No se pudieron buscar rutas: {error}")
        finally:
            self._search_btn.setEnabled(True)
            self._on_route_selected()

    def _fill_table(self, comp: BridgeComparison) -> None:
        self._table.setRowCount(0)
        self._pair.setText(comp.request.symbol)
        for route in comp.routes:
            quote = route.quote
            row = self._table.rowCount()
            self._table.insertRow(row)
            posicion = QTableWidgetItem(str(route.position))
            if route.is_best:
                # La primera fila es la mejor ruta: en verde, para que se lea sin
                # comparar las cifras una a una.
                posicion.setForeground(QColor(COLOR_SUCCESS))
            self._table.setItem(row, 0, posicion)
            self._table.setItem(row, 1, QTableWidgetItem(quote.provider))
            self._table.setItem(row, 2, QTableWidgetItem(quote.engine_id))
            out = QTableWidgetItem(str(quote.amount_out))
            if route.is_best:
                out.setForeground(QColor(COLOR_SUCCESS))
            self._table.setItem(row, 3, out)
            self._table.setItem(row, 4, QTableWidgetItem(str(quote.amount_out_min)))
            comision = (
                f"{quote.fee_bps} [{quote.fee_basis.value}]"
                if quote.fee_bps is not None and quote.fee_basis is not None
                else "— no desglosada"
            )
            item_fee = QTableWidgetItem(comision)
            if quote.fee_bps is None:
                # Naranja y no gris: una comisión sin declarar hace que la ruta
                # parezca gratis, y elegir la que parece gratis es el error que
                # este color evita.
                item_fee.setForeground(QColor(COLOR_WARNING))
            self._table.setItem(row, 5, item_fee)
            self._table.setItem(row, 6, QTableWidgetItem(quote.duration_label))
            nota = QTableWidgetItem(quote.source_note or quote.route)
            nota.setToolTip(quote.source_note or quote.route)
            self._table.setItem(row, 7, nota)
        set_empty(self._table, self._empty, "Ningún motor devolvió una ruta para este cruce.")
        _cap_height(self._table)

    def _clear_table(self, motivo: str) -> None:
        self._table.setRowCount(0)
        self._pair.setText("—")
        self._out_estimate.setText("—")
        set_empty(self._table, self._empty, motivo)
        _cap_height(self._table)

    # ------------------------------------------------------------------ #
    # La ruta elegida y lo que falta para firmarla
    # ------------------------------------------------------------------ #
    def _selected_quote(self) -> BridgeQuote | None:
        model = self._table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows or self._comparison is None:
            return None
        index = rows[0].row()
        routes = self._comparison.routes
        return routes[index].quote if 0 <= index < len(routes) else None

    def _on_route_selected(self) -> None:
        quote = self._selected_quote()
        self._prepare_btn.setEnabled(
            quote is not None
            and self._container.prepare_bridge.is_available(quote.request.origin.chain)
        )
        motivos = self._blockers(quote)
        self._exec_btn.setEnabled(quote is not None and not motivos)

        if quote is None:
            self._chosen.setText(
                "Selecciona una ruta para prepararla o firmarla."
                if self._table.rowCount() > 0
                else ""
            )
            self._chosen.setToolTip("")
            self._out_estimate.setText("—")
        else:
            resumen = (
                f"{quote.provider} · motor {quote.engine_id} · cruzas "
                f"{quote.request.symbol} · entregas {quote.request.amount_in} y "
                f"recibes {quote.amount_out} · {quote.duration_label}"
            )
            self._chosen.setText(f"<b>Ruta elegida:</b> {resumen}")
            # La línea no se envuelve, así que en una ventana estrecha se recorta.
            # Aquí está la frase entera, para que recortar no sea perder.
            self._chosen.setToolTip(f"Ruta elegida: {resumen}")
            self._out_estimate.setText(f"≈ {quote.amount_out}")

        self._exec_note.setText(
            ""
            if quote is None or not motivos
            else "No se puede ejecutar: " + " · ".join(motivos) + "."
        )
        self._refresh_upgrade_offer(motivos)

    def _blockers(self, quote: BridgeQuote | None) -> tuple[str, ...]:
        """Todo lo que falta para poder firmar el cruce. Vacío significa que sí.

        Se devuelven **todos** los motivos y no el primero: son condiciones
        distintas —el modo se cambia aquí, `enabled` se cambia en el fichero, las
        redes y los tokens se declaran en la configuración— y descubrirlas de una
        en una, arreglando una para que aparezca la siguiente, es lo que hace
        pensar que la aplicación está rota en vez de a medio configurar.

        Mira lo mismo que mira `ExecuteBridge` antes de firmar, en el mismo orden,
        y reutiliza **las comprobaciones de verdad** en vez de reescribir sus
        reglas: si esta lista dijera que sí y el caso de uso dijera que no, la
        pantalla estaría prometiendo algo que no cumple.
        """
        container = self._container
        limits = container.policy.limits
        motivos: list[str] = []
        if not container.guard.mode.grants(Capability.BROADCAST_TX):
            motivos.append(
                f"el modo {container.guard.mode.label} no permite emitir "
                "(cambia a EJECUCIÓN)"
            )
        if not limits.enabled:
            motivos.append(
                "la ejecución está apagada (`enabled = true` bajo `[execution]` "
                "en config.toml)"
            )

        request = quote.request if quote is not None else self._request()[0]
        if request is not None:
            motivos.extend(self._limit_blockers(request))
        chain_key = request.origin.chain if request is not None else None
        if not container.prepare_bridge.is_available(chain_key):
            motivos.append(self._por_que_no_hay_planificador(chain_key))
        if not container.keys.available():
            motivos.append("no hay cartera configurada, así que no hay con qué firmar")
        return tuple(motivos)

    def _por_que_no_hay_planificador(self, chain_key: str | None) -> str:
        """Por qué no hay quien construya este puente, y qué se puede hacer.

        Se distinguen dos casos que antes se decían igual: que **no haya ningún**
        motor de puentes activo, y que los que hay no lleguen a esa red. El texto
        de antes ofrecía siempre lo mismo —«activa `lifi` o `relay`»— y con `lifi`
        ya activo eso manda al usuario a un panel donde no hay nada que activar:
        el motor está, lo que no cubre es esa red. La salida es otra —cambiar el
        origen—, así que el mensaje tiene que ser otro.
        """
        try:
            activos = self._container.registry.bridge_planners()
        except NoActiveEngineError:
            # La ranura entera está vacía: aquí sí hay algo que activar.
            activos = ()
        if not activos:
            return (
                "no hay ningún motor de puentes activo "
                "(activa `lifi` o `relay` en el panel de motores)"
            )
        nombre = ", ".join(sorted(engine.manifest.engine_id for engine in activos))
        donde = f" desde «{chain_key}»" if chain_key else ""
        return (
            f"los motores de puentes activos ({nombre}) no cruzan{donde}: "
            "elige otra red de origen o activa uno que la cubra"
        )

    def _limit_blockers(self, request: BridgeRequest) -> tuple[str, ...]:
        """Lo que los topes dirían de este cruce, con las **dos** redes.

        La de destino se comprueba aquí y no sólo la de origen, que es lo que
        distingue un puente de un swap: el dinero no se queda en la red de la que
        sale, así que una lista blanca que sólo cubriera ésa dejaría cruzar hacia
        una red que el usuario no autorizó. Es el motivo que más se va a encontrar
        quien pruebe esto por primera vez, porque la configuración por defecto no
        tiene ni `base` ni `polygon` declaradas.
        """
        limits = self._container.policy.limits
        motivos: list[str] = []
        try:
            limits.check_token(
                tuple(dict.fromkeys((request.origin.symbol, request.destination.symbol)))
            )
        except ExecutionLimitExceededError as error:
            motivos.append(str(error))
        for chain_key in request.chains:
            try:
                limits.check_chain(chain_key)
            except ExecutionLimitExceededError as error:
                motivos.append(str(error))

        notional = bridge_notional(request)
        if notional is None:
            # La pata de origen no es la moneda de los topes, así que el importe hay
            # que valorarlo cotizando y el repintado de un botón no puede permitirse
            # una petición de red. No se calla del todo: se dice que el tope se
            # aplicará al firmar, y que si no se puede valorar no se cruzará. Un
            # botón encendido sin esta línea prometería un cruce que puede negarse.
            motivos.append(
                f"el importe sale en {request.origin.symbol}, que no es la moneda de "
                f"los topes de {request.origin.chain}: se valorará al firmar, y si no "
                f"se puede valorar no se cruzará"
            )
            return tuple(motivos)
        try:
            limits.check_amount(
                notional.as_decimal(),
                spent_today=self._container.policy.spent_in_window(notional.symbol),
            )
        except ExecutionLimitExceededError as error:
            motivos.append(str(error))
        return tuple(motivos)

    def _refresh_upgrade_offer(self, motivos: tuple[str, ...]) -> None:
        """Ofrece el cambio de modo sólo cuando el modo es lo que bloquea.

        Con otro motivo de por medio —una red sin declarar, por ejemplo— cambiar de
        modo no desbloquearía nada, y un botón que no arregla lo que promete es peor
        que no tenerlo.
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

    def _on_upgrade_mode(self) -> None:
        destino = self._container.guard.upgrade_path(Capability.BROADCAST_TX)
        if destino is None:
            return
        self._container.guard.set_mode(destino)
        self._status.setText(
            f"Modo cambiado a {destino.label}. La ejecución sigue dependiendo de los "
            "topes y del interruptor de `config.toml`."
        )
        self._on_route_selected()

    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo, que son los dos botones."""
        self._on_route_selected()

    # ------------------------------------------------------------------ #
    # Preparar y ejecutar
    # ------------------------------------------------------------------ #
    def _ask_recipient(self, chain_key: str) -> str | None:
        """La dirección que recibe **en la red de destino**.

        Se ofrecen las propias y se admite una pegada: teclear una dirección a mano
        es la forma más común de perder fondos, y en un puente la dirección de
        destino es literalmente donde va a aparecer el dinero.
        """
        spec = chain(chain_key)
        if spec.address_format is not AddressFormat.EVM:
            # Las dos patas de un puente tienen que ser EVM hoy: `ExecuteBridge`
            # firma en EVM y lo comprueba antes de construir. Se dice aquí en vez de
            # dejar que falle al pulsar.
            self._status.setText(
                f"«{chain_key}» no es una red EVM y este camino todavía no firma ahí."
            )
            return None
        candidatos: list[str] = []
        propia = self._container.keys.address()
        if propia is not None and is_evm_address(propia):
            candidatos.append(propia)
        candidatos.extend(
            address
            for address in self._container.settings.watch_addresses
            if is_evm_address(address)
        )
        candidatos = list(dict.fromkeys(candidatos))
        title = "Destino del puente"
        if candidatos:
            text, accepted = QInputDialog.getItem(
                self,
                title,
                f"Dirección que recibe en {chain_key} (elige una tuya o pega otra):",
                candidatos,
                0,
                True,
            )
        else:
            text, accepted = QInputDialog.getText(
                self, title, f"Dirección que recibe en {chain_key}:"
            )
        if not accepted:
            return None
        return text.strip() or None

    def _on_prepare(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una ruta de la tabla.")
            return
        recipient = self._ask_recipient(quote.request.destination.chain)
        if recipient is None:
            return
        self._prepare_btn.setEnabled(False)
        self._status.setText("Construyendo la transacción sin firmar…")
        spawn(self._do_prepare(quote, recipient))

    async def _do_prepare(self, quote: BridgeQuote, recipient: str) -> None:
        try:
            transaction = await self._container.prepare_bridge(quote, recipient=recipient)
            self._status.setText(
                f"Transacción preparada y confirmada (destino {transaction.to_address}). "
                "Amigocompora NO la ha firmado ni emitido: no sale dinero de ninguna red."
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._status.setText("Cancelaste la preparación. No se construyó nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
        finally:
            self._on_route_selected()

    def _on_execute(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una ruta de la tabla.")
            return
        # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el modo
        # se puede cambiar mientras esta pestaña está abierta, y un botón sólo se
        # repinta cuando algo lo repinta. La puerta de verdad está en `ExecuteBridge`.
        motivos = self._blockers(quote)
        if motivos:
            self._status.setText("No se puede ejecutar: " + " · ".join(motivos) + ".")
            return
        recipient = self._ask_recipient(quote.request.destination.chain)
        if recipient is None:
            return
        self._exec_btn.setEnabled(False)
        self._status.setText("Firmando y emitiendo el cruce…")
        spawn(self._do_execute(quote, recipient))

    async def _do_execute(self, quote: BridgeQuote, recipient: str) -> None:
        try:
            receipt = await self._container.execute_bridge(quote, recipient=recipient)
            self._status.setText(
                f"Emitida {receipt.tx_hash} · estado {receipt.status.value}. El dinero "
                f"salió de {quote.request.origin.chain}: el tiempo del cruce lo pone el "
                "proveedor, no la aplicación."
            )
        except ConfirmationDeniedError:
            self._status.setText("Cancelaste la operación. No se firmó ni se emitió nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
        finally:
            self._on_route_selected()


def _position_of(tokens: tuple[Token, ...], token: Token | None) -> int:
    """Dónde está un token en la tupla, por identidad y no por etiqueta.

    Se busca por `is_same_asset` y no por el texto del desplegable: las etiquetas
    no identifican unívocamente —`USDC` puede ser dos tokens— y el texto es
    presentación, así que si cambia el formato la elección del usuario no tiene
    por qué perderse. Devuelve 0 cuando no está: la primera entrada de la lista es
    una elección válida, y un índice negativo no lo sería.
    """
    if token is None:
        return 0
    for index, candidato in enumerate(tokens):
        if candidato.is_same_asset(token):
            return index
    return 0


def _summary(comp: BridgeComparison) -> str:
    """Lo que se dice bajo el botón después de buscar: cuántas y con qué límites.

    Se dice cuando falta una fuente —«sin respuesta: relay»— porque una tabla a la
    que le falta un motor se lee como «esto es todo lo que hay», y puede que la
    mejor ruta fuera justo la del que no contestó.
    """
    nota = ""
    if comp.has_unknown_fees:
        nota += " · hay comisiones no desglosadas"
    if comp.has_partial_sources:
        nota += f" · sin respuesta: {', '.join(comp.failed_engines)}"
    return f"{len(comp.routes)} ruta(s) · motor(es) {comp.request.chains[0]}→" f"{comp.request.chains[1]}{nota}"


def _cap_height(table: QTableWidget, filas: int = 8) -> None:
    """Le pone techo al alto de la tabla: el de sus filas, no el que le sobre.

    Es un **máximo**, no un alto fijo: por debajo encoge sola, que es lo que hace
    falta cuando la ventana es pequeña o cuando no hay filas con las que medir.
    """
    visible = min(table.rowCount(), filas)
    alto = table.horizontalHeader().height() + 2 * table.frameWidth()
    for row in range(visible):
        alto += table.rowHeight(row)
    table.setMaximumHeight(alto if visible else 0)
