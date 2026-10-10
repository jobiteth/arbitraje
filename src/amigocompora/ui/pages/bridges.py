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

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.execute_bridge import bridge_notional
from amigocompora.app.usecases.track_bridge import TrackedBridge, new_tracked_bridge
from amigocompora.domain.addresses import is_evm_address
from amigocompora.domain.chains import CHAINS, AddressFormat, chain
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
)
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    Token,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind
from amigocompora.engines.catalog import quote_token
from amigocompora.ui import icons
from amigocompora.ui.bridge_progress_dialog import (
    BridgeProgressDialog,
    acortar,
    enlace,
    enlace_lifi,
    estado_visual,
)
from amigocompora.ui.execution_gate import bridge_blockers
from amigocompora.ui.theme import COLOR_DANGER, COLOR_MUTED, COLOR_SUCCESS, COLOR_WARNING
from amigocompora.ui.wallet_state import WalletBalances
from amigocompora.ui.widgets import (
    AmountSpinBox,
    Card,
    divider,
    format_amount,
    needed_width,
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

#: Lo que hace «Máx», dicho donde se puede leer **antes** de pulsarlo. Vive en una
#: constante porque lo dicen dos sitios —el botón y el aviso de cuando está
#: apagado— y dos textos distintos para lo mismo acaban diciendo cosas distintas.
_MAX_AYUDA: Final = (
    "Pone todo el saldo disponible de esta red. El campo tiene seis decimales, así "
    "que de un token de dieciocho deja fuera lo que no cabe —siempre por debajo "
    "del saldo, nunca por encima."
)


class _LegBox(QFrame):
    """El recuadro de una pata del cruce: la red arriba, el importe y el token en
    el centro, el saldo abajo.

    Es **la misma caja** que las dos patas de la tarjeta de swap —lleva el
    `objectName` «legBox», así que el aspecto lo pone el tema en un único sitio— y
    por la misma razón: lo que se está describiendo es un paso de una cosa a otra,
    y las dos puntas tienen que verse como dos puntas y no como cuatro campos de
    un formulario, que es lo que había aquí.

    No se reutiliza la clase de swap (`TokenLeg`) porque las patas de un puente
    llevan **red**, que las de un swap no tienen —el swap pasa dentro de una—, y
    porque aquí el token se elige en un desplegable y no en el modal del selector:
    la lista de una red es corta y la decisión es «qué moneda cruzo», no «cuál de
    los cien mints de mi cartera».
    """

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("legBox")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(6)

        #: La cabecera: el rótulo, la red y los mandos de la pata («Máx»).
        self.header = QHBoxLayout()
        self.header.setContentsMargins(0, 0, 0, 0)
        self.header.setSpacing(8)
        titulo = QLabel(caption)
        titulo.setObjectName("sectionTitle")
        self.header.addWidget(titulo)
        lay.addLayout(self.header)

        #: La fila del importe: la cifra a la izquierda y el token a la derecha.
        self.row = QHBoxLayout()
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        lay.addLayout(self.row)

        #: El saldo de esta pata, dentro de la caja y no en una fila aparte: el
        #: saldo es de esta red y de este token, y en una fila común debajo de las
        #: dos no se sabe de cuál habla cada mitad. Comparte fila con los mandos
        #: que operan sobre él («Máx»): el atajo vive pegado a la cifra que copia.
        self.foot = QHBoxLayout()
        self.foot.setContentsMargins(0, 0, 0, 0)
        self.foot.setSpacing(8)
        self.balance = QLabel("")
        self.balance.setObjectName("hint")
        self.balance.setWordWrap(True)
        self.foot.addWidget(self.balance, 1)
        lay.addLayout(self.foot)


class BridgesSection(QWidget):
    """La tarjeta «Entre redes» dentro de la pestaña de swap."""

    def __init__(
        self,
        container: Container,
        parent: QWidget | None = None,
        *,
        balances: WalletBalances | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: BridgeComparison | None = None
        #: Si hay una ejecución en vuelo. Apaga el botón de ejecutar mientras
        #: dure: sin esto, `_on_route_selected` volvía a encenderlo al repintar
        #: y un segundo clic firmaba otra vez con el mismo nonce (medido el
        #: 2026-10-10). La puerta de verdad está en `ExecuteBridge` —ver
        #: `SingleFlight`—; esto es la parte que se ve.
        self._executing = False
        self._tracking = container.bridge_tracking
        self._tracking_resumed = False
        self._tx_hashes: list[str] = []
        self._tx_dialogs: dict[str, BridgeProgressDialog] = {}
        self._tracking.subscribe(self._on_tracked)
        #: Si ya se ajustó el ancho del campo de importe. Ver `showEvent`.
        self._amount_sized = False
        #: Los mismos saldos que la tarjeta de swap: lo que una vista lee, la otra lo
        #: ve sin volver a preguntar al nodo.
        self._balances = balances or WalletBalances(container.read_wallet)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build())

        self._origin_chain.currentIndexChanged.connect(self._refresh_tokens)
        self._destination_chain.currentIndexChanged.connect(self._refresh_tokens)
        self._origin_token.currentIndexChanged.connect(lambda _index: self._refresh_balances())
        self._destination_token.currentIndexChanged.connect(
            lambda _index: self._refresh_balances()
        )
        self._balances.subscribe(self._on_balance_read)
        self._refresh_tokens()
        self._on_route_selected()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._tracking_resumed:
            self._tracking_resumed = True
            spawn(self._resume_tracking())
        """Ajusta el ancho del campo de importe cuando ya hay estilo aplicado.

        El ancho se mide **aquí** y no en `_build` porque medirlo antes da un
        número equivocado: el widget todavía no ha heredado la hoja de estilos y su
        fuente no es la que va a usar. Medido en las pruebas: en construcción el
        campo declara 204 px y una vez aplicado el estilo necesita 257.

        El error es silencioso por partida doble: desde Qt 6.0 ya no se emite el
        aviso de «sizeHint consultado antes de pulir», y el síntoma es un campo más
        estrecho que su contenido — exactamente el defecto que la prueba de
        recorte persigue. Se hace en el primer `showEvent`, que es tarde por
        definición, y sólo una vez.
        """
        super().showEvent(event)
        if self._amount_sized:
            return
        self._amount_sized = True
        self._amount.setMinimumWidth(needed_width(self._amount))

    # ------------------------------------------------------------------ #
    # Construcción
    # ------------------------------------------------------------------ #
    def _build(self) -> QWidget:
        card = Card(
            "Entre redes",
            subtitle="— cruzar de una red a otra; las rutas se piden a todos los motores activos",
        )

        self._origin_chain = self._chain_combo(_ORIGEN_POR_DEFECTO)
        self._origin_chain.setObjectName("chainPill")
        self._origin_token = QComboBox()
        self._origin_token.setObjectName("tokenPill")
        self._amount = AmountSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        # El importe es **lo que se escribe** en esta pantalla, así que va en grande
        # y sin caja: el recuadro de la pata ya dice dónde se escribe, y una caja
        # dentro de otra es lo que hacía que esto pareciera un formulario.
        self._amount.setObjectName("amountInput")
        self._amount.setButtonSymbols(AmountSpinBox.ButtonSymbols.NoButtons)
        self._amount.setFrame(False)
        # El ancho de este campo se ajusta al mostrarlo, no aquí: medirlo en
        # construcción da un número que no vale. Ver `showEvent`.
        # No hay rótulo de unidad al lado del importe: el símbolo lo lleva la
        # píldora del token, a dos dedos a la derecha, y escribirlo dos veces en la
        # misma fila es ruido —además de dos sitios donde equivocarse—.
        self._max_btn = QPushButton("Máx")
        self._max_btn.setObjectName("link")
        self._max_btn.setToolTip(_MAX_AYUDA)
        self._max_btn.clicked.connect(self._on_max)

        origen = _LegBox("ENTREGAS EN")
        origen.header.addWidget(self._origin_chain)
        origen.header.addStretch()
        origen.row.addWidget(self._amount, 1)
        origen.row.addWidget(self._origin_token, 0, Qt.AlignVCenter)
        origen.foot.addWidget(self._max_btn, 0, Qt.AlignVCenter)
        self._origin_balance = origen.balance
        card.body().addWidget(origen)

        # El mando de invertir, centrado entre las dos patas: ahí es donde se lee
        # la relación entre ellas. Lleva la palabra y no el glifo «⇅» por lo mismo
        # que en la tarjeta de swap: dibujado con la fuente del sistema sale un
        # signo diminuto que se lee como una letra suelta.
        invertir = QHBoxLayout()
        invertir.addStretch()
        self._invert_btn = QPushButton("Invertir")
        self._invert_btn.setObjectName("invert")
        self._invert_btn.setToolTip(
            "Cambia las dos patas de sitio: la red y el token de origen pasan a "
            "destino, y los de destino a origen. No firma ni emite nada."
        )
        self._invert_btn.clicked.connect(self._on_invert)
        invertir.addWidget(self._invert_btn)
        invertir.addStretch()
        card.add_row(invertir)

        self._destination_chain = self._chain_combo(_DESTINO_POR_DEFECTO)
        self._destination_chain.setObjectName("chainPill")
        self._destination_token = QComboBox()
        self._destination_token.setObjectName("tokenPill")
        self._out_estimate = QLabel("—")
        self._out_estimate.setObjectName("bigNumber")
        self._out_estimate.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._out_estimate.setMinimumWidth(150)
        self._set_estimate(None)

        destino = _LegBox("RECIBES EN")
        destino.header.addWidget(self._destination_chain)
        destino.header.addStretch()
        destino.row.addWidget(self._out_estimate, 1)
        destino.row.addWidget(self._destination_token, 0, Qt.AlignVCenter)
        self._destination_balance = destino.balance
        card.body().addWidget(destino)

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
        _cabeceras(self._table, _COLUMNAS_RUTAS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.itemSelectionChanged.connect(self._on_route_selected)
        card.body().addWidget(self._table, stretch=1)

        # Un motor sin ruta no aparece en la tabla de arriba, y sin esta lista no se
        # distingue «no ha contestado» de «no está activo». Va debajo, como en swaps.
        self._engines = QTableWidget(0, 4)
        self._engines.setHorizontalHeaderLabels(["Motor", "Estado", "Recibes", "Nota"])
        self._engines.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._engines.setAlternatingRowColors(True)
        self._engines.setEditTriggers(QTableWidget.NoEditTriggers)
        self._engines.setSelectionMode(QTableWidget.NoSelection)
        self._engines.setShowGrid(False)
        self._engines.verticalHeader().setVisible(False)
        self._engines.setVisible(False)
        self._engines_toggle = QPushButton("Mostrar motores")
        self._engines_toggle.setObjectName("secondary")
        self._engines_toggle.setCheckable(True)
        self._engines_toggle.toggled.connect(self._on_engines_toggled)
        card.body().addWidget(self._engines_toggle, alignment=Qt.AlignLeft)
        card.body().addWidget(self._engines)

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

        card.body().addWidget(divider())
        card.body().addWidget(self._build_transactions())

        card.setSizePolicy(card.sizePolicy().horizontalPolicy(), card.sizePolicy().verticalPolicy())
        return card

    def _build_transactions(self) -> QWidget:
        bloque = QWidget()
        lay = QVBoxLayout(bloque)
        lay.setContentsMargins(0, 0, 0, 0)
        titulo = QLabel("Transacciones")
        titulo.setObjectName("cardHeader")
        lay.addWidget(titulo)
        self._tx = QTableWidget(0, 7)
        self._tx.setHorizontalHeaderLabels(
            ["Estado", "Ruta", "Importe", "Origen", "Destino", "Seguimiento", ""]
        )
        self._tx.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._tx.setAlternatingRowColors(True)
        self._tx.setEditTriggers(QTableWidget.NoEditTriggers)
        self._tx.setSelectionMode(QTableWidget.NoSelection)
        self._tx.setShowGrid(False)
        self._tx.verticalHeader().setVisible(False)
        self._tx_empty = QLabel("")
        self._tx_empty.setObjectName("empty")
        self._tx_empty.setWordWrap(True)
        lay.addWidget(self._tx)
        lay.addWidget(self._tx_empty)
        self._refresh_transactions()
        return bloque

    @staticmethod
    def _chain_combo(seleccionada: str) -> QComboBox:
        combo = QComboBox()
        for key in sorted(CHAINS):
            combo.addItem(icons.network_icon(key), f"{CHAINS[key].name} ({key})", key)
        index = combo.findData(seleccionada)
        if index >= 0:
            combo.setCurrentIndex(index)
        # Al ancho de su contenido, no al de la fila: un `QComboBox` es `Expanding`
        # por omisión y en la cabecera de la pata se comía el hueco entero, dejando
        # el rótulo pegado a un campo vacío de media tarjeta. Se mide contra el
        # nombre más largo de la lista, así que el ancho no baila al cambiar de red.
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
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
        self._refresh_balances()
        self._on_route_selected()

    # ------------------------------------------------------------------ #
    # Saldos
    # ------------------------------------------------------------------ #
    def _refresh_balances(self, *, forced: bool = False) -> None:
        """Pide los saldos de las dos redes elegidas y pinta lo que haya.

        `forced` relee aunque la caché tenga datos: se usa tras emitir, porque el
        saldo que había antes ya no es el real.
        """
        owner = self._container.keys.address() or ""
        redes = {self._chain_key(self._origin_chain), self._chain_key(self._destination_chain)}
        for chain_key in redes:
            if not chain_key:
                continue
            if forced:
                self._balances.refresh(chain_key, owner=owner)
            else:
                self._balances.ensure(chain_key, owner=owner)
        self._paint_balances()

    def _on_balance_read(self, _chain_key: str) -> None:
        self._paint_balances()

    def _paint_balances(self) -> None:
        self._paint_side(self._origin_token, self._origin_chain, self._origin_balance)
        self._paint_side(
            self._destination_token, self._destination_chain, self._destination_balance
        )
        self._refresh_max()

    def _refresh_max(self) -> None:
        """Apaga «Máx» cuando no hay saldo que poner, y dice por qué está apagado.

        Se apaga en vez de desaparecer: un botón que va y viene según el saldo deja
        al usuario sin saber si la función existe.
        """
        token = self._origin_token.currentData()
        holding = (
            self._balances.find(self._chain_key(self._origin_chain), token)
            if isinstance(token, Token)
            else None
        )
        hay_saldo = holding is not None and not holding.is_empty
        self._max_btn.setEnabled(hay_saldo)
        self._max_btn.setToolTip(
            _MAX_AYUDA
            if hay_saldo
            else (
                "No hay saldo leído de esta red y este token, así que no hay nada "
                "que poner. «Sin leer» y «cero» no son lo mismo: mira la línea de "
                "saldo de la pata."
            )
        )

    def _on_max(self) -> None:
        """Pone en el importe todo el saldo disponible, y dice lo que no cabe.

        Se trunca **hacia abajo**. El campo redondea al más cercano y eso puede dar
        un número mayor que el saldo —medido en la tarjeta de swap: 0,0928237 pasó a
        0,092824, por encima de lo que había, y la operación revirtió con
        `TRANSFER_FROM_FAILED`—. Quedarse corto se arregla escribiendo; pasarse se
        descubre cuando ya se ha firmado.
        """
        token = self._origin_token.currentData()
        if not isinstance(token, Token):
            return
        holding = self._balances.find(self._chain_key(self._origin_chain), token)
        if holding is None or holding.is_empty:
            return
        completo = holding.as_decimal()
        por_debajo = completo.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if por_debajo < Decimal(str(self._amount.minimum())):
            self._status.setText(
                f"El saldo de {token.symbol} en {token.chain} es "
                f"{format_amount(completo, full=True)}: menos que el importe más "
                f"pequeño que admite el campo."
            )
            return
        self._amount.setValue(float(por_debajo))
        escrito = Decimal(str(self._amount.value()))
        self._status.setText(
            ""
            if escrito >= completo
            else (
                f"Máx: {format_amount(escrito, full=True)} de "
                f"{format_amount(completo, full=True)} {token.symbol} — el campo "
                f"tiene seis decimales y no cabe el resto."
            )
        )

    def _paint_side(self, token_combo: QComboBox, chain_combo: QComboBox, label: QLabel) -> None:
        token = token_combo.currentData()
        chain_key = self._chain_key(chain_combo)
        if not isinstance(token, Token) or not self._container.keys.address():
            label.setText("")
            label.setToolTip("")
            return
        leido = self._balances.holdings(chain_key)
        if leido is None:
            label.setText("Leyendo el saldo…" if self._balances.reading(chain_key) else "")
            label.setToolTip("")
            return
        if leido.failed:
            label.setText("Saldo no disponible")
            label.setStyleSheet(f"color: {COLOR_DANGER};")
            label.setToolTip(leido.error or "")
            return
        holding = self._balances.find(chain_key, token)
        if holding is None:
            label.setText(f"Sin saldo de {token.symbol}")
            label.setStyleSheet(f"color: {COLOR_MUTED};")
            label.setToolTip("")
            return
        label.setText(f"Disponible: {format_amount(holding.as_decimal())} {token.symbol}")
        label.setStyleSheet("")
        label.setToolTip(f"{holding.as_decimal():f} {token.symbol}")

    def _reload(self, combo: QComboBox, chain_key: str) -> None:
        previous = combo.currentData()
        tokens = tokens_for_chain(self._container.token_store, chain_key)
        combo.blockSignals(True)
        combo.clear()
        for label, token in zip(token_labels(tokens), tokens, strict=True):
            combo.addItem(icons.token_icon(token.display_symbol), label, token)
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

    def _on_invert(self) -> None:
        """Cambia las dos patas de sitio, cada una con su red y su token.

        Las listas se rearman **una sola vez** y después se coloca cada token en la
        pata nueva: `_reload` conserva el token que ya había en cada desplegable, y
        tras invertir ése es justo el que no toca. El token no se busca con
        `is_same_asset` —compara la red, así que nunca acertaría al cruzarla— sino
        por su equivalente en la red de enfrente.
        """
        origen_red = self._origin_chain.currentIndex()
        destino_red = self._destination_chain.currentIndex()
        origen_token = self._origin_token.currentData()
        destino_token = self._destination_token.currentData()
        for combo, index in (
            (self._origin_chain, destino_red),
            (self._destination_chain, origen_red),
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self._refresh_tokens()
        _select_equivalent(self._origin_token, destino_token)
        _select_equivalent(self._destination_token, origen_token)

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
            cotiza = QTableWidgetItem(_motor_label(quote.engine_id))
            cotiza.setIcon(icons.brand_icon(quote.engine_id))
            cotiza.setToolTip(f"Motor de Amigocompora: {quote.engine_id}")
            self._table.setItem(row, 2, cotiza)
            out = _cifra_item(quote.amount_out)
            if route.is_best:
                out.setForeground(QColor(COLOR_SUCCESS))
            self._table.setItem(row, 3, out)
            self._table.setItem(row, 4, _cifra_item(quote.amount_out_min))
            comision = (
                f"{quote.fee_bps} [{quote.fee_basis.value}]"
                if quote.fee_bps is not None and quote.fee_basis is not None
                else "— no desglosada"
            )
            item_fee = QTableWidgetItem(comision)
            item_fee.setTextAlignment(_DERECHA)
            if quote.fee_bps is None:
                # Naranja y no gris: una comisión sin declarar hace que la ruta
                # parezca gratis, y elegir la que parece gratis es el error que
                # este color evita.
                item_fee.setForeground(QColor(COLOR_WARNING))
            self._table.setItem(row, 5, item_fee)
            duracion = QTableWidgetItem(quote.duration_label)
            duracion.setTextAlignment(_DERECHA)
            self._table.setItem(row, 6, duracion)
            self._table.setItem(row, 7, _nota_item(quote.source_note or quote.route))
        set_empty(self._table, self._empty, "Ningún motor devolvió una ruta para este cruce.")
        _cap_height(self._table)
        self._fill_engines(comp)

    def _on_engines_toggled(self, abierto: bool) -> None:
        self._engines.setVisible(abierto)
        self._engines_toggle.setText("Ocultar motores" if abierto else "Mostrar motores")

    def _fill_engines(self, comp: BridgeComparison) -> None:
        mejor: dict[str, TokenAmount] = {}
        for route in comp.routes:
            mejor.setdefault(route.quote.engine_id, route.quote.amount_out)
        activos = {
            planner.manifest.engine_id for planner in self._container.registry.bridge_planners()
        }
        self._engines.setRowCount(0)
        for entrada in self._container.registry.available():
            manifest = entrada.manifest
            if manifest.kind is not EngineKind.CROSS_CHAIN:
                continue
            fila = self._engines.rowCount()
            self._engines.insertRow(fila)
            activo = manifest.engine_id in activos
            motor = QTableWidgetItem(_motor_label(manifest.engine_id))
            motor.setIcon(icons.brand_icon(manifest.engine_id))
            self._engines.setItem(fila, 0, motor)
            self._engines.setItem(fila, 1, QTableWidgetItem("activo" if activo else "inactivo"))
            monto = mejor.get(manifest.engine_id)
            if monto is not None:
                nota = "mejor ruta de este motor"
                recibes = _cifra_item(monto)
            else:
                if manifest.engine_id in comp.failed_engines:
                    nota = "sin respuesta"
                elif not activo:
                    nota = "inactivo: actívalo en el panel de Motores"
                else:
                    nota = "sin ruta para este cruce"
                recibes = QTableWidgetItem("—")
                recibes.setTextAlignment(_DERECHA)
            self._engines.setItem(fila, 2, recibes)
            self._engines.setItem(fila, 3, _nota_item(nota))
        _cap_height(self._engines)

    def _clear_table(self, motivo: str) -> None:
        self._table.setRowCount(0)
        self._engines.setRowCount(0)
        self._pair.setText("—")
        self._set_estimate(None)
        set_empty(self._table, self._empty, motivo)
        _cap_height(self._table)

    def _set_estimate(self, amount: TokenAmount | None) -> None:
        """La cifra de la pata de destino: la de la ruta elegida, o un hueco.

        Va **sin símbolo** porque el token ya está en el desplegable de al lado, y
        con el importe exacto en el tooltip: lo que se enseña está recortado a seis
        cifras significativas y lo que se firma no. Apagada en gris mientras no hay
        ruta, para que un hueco no se lea como un resultado.
        """
        if amount is None:
            self._out_estimate.setText("—")
            self._out_estimate.setStyleSheet(f"color: {COLOR_MUTED};")
            self._out_estimate.setToolTip(
                "Lo que devolvería la ruta elegida. Se rellena al buscar."
            )
            return
        self._out_estimate.setText(f"≈ {format_amount(amount.as_decimal())}")
        self._out_estimate.setStyleSheet("")
        self._out_estimate.setToolTip(f"Exacto: {amount}")

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
        # Los avisos no apagan el botón: informan de lo que decidirá la firma (hoy,
        # que el importe se valorará al firmar). Ver `_notes`.
        notas = self._notes(quote)
        self._exec_btn.setEnabled(quote is not None and not motivos and not self._executing)

        if quote is None:
            self._chosen.setText(
                "Selecciona una ruta para prepararla o firmarla."
                if self._table.rowCount() > 0
                else ""
            )
            self._chosen.setToolTip("")
            self._set_estimate(None)
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
            self._set_estimate(quote.amount_out)

        if quote is not None and motivos:
            self._exec_note.setStyleSheet(f"color: {COLOR_DANGER};")
            self._exec_note.setText("No se puede ejecutar: " + " · ".join(motivos) + ".")
        elif notas:
            # En ámbar y sin el «no se puede»: el botón sigue encendido, porque lo
            # que anuncia el aviso lo decidirá la firma con la cotización delante.
            self._exec_note.setStyleSheet(f"color: {COLOR_WARNING};")
            self._exec_note.setText("Aviso: " + " · ".join(notas) + ".")
        else:
            self._exec_note.setText("")
        self._refresh_upgrade_offer(motivos)

    def _blockers(self, quote: BridgeQuote | None) -> tuple[str, ...]:
        """Todo lo que falta para poder firmar el cruce. Vacío significa que sí.

        Se delega en `ui/execution_gate.py`, que es donde vive la comprobación
        desde que el copiloto también puede ofrecer un cruce desde el chat: la
        misma ruta puede salir de dos pantallas, y las dos tienen que decir lo
        mismo antes de firmar. La petición se le pasa ya construida con los
        controles —y no se deduce del texto de la tabla— para que la lista hable
        de **esta** operación, la que el usuario escribió.

        Cuando todavía no hay ruta elegida, la petición sale igualmente de los
        controles: así los motivos que no dependen de la ruta —el modo, el
        interruptor, las redes, la cartera— se ven antes de cotizar, que es
        cuando sirven para algo.
        """
        peticion = self._request()[0] if quote is None else quote.request
        return bridge_blockers(self._container, request=peticion, quote=quote)

    def _notes(self, quote: BridgeQuote | None) -> tuple[str, ...]:
        """Lo que conviene saber antes de firmar, sin apagar el botón.

        Hoy una sola: la pata que se entrega no es la moneda de los topes de su
        red, así que el importe se valorará al firmar —y si no se puede valorar,
        no se cruzará—. El repintado no puede hacer esa valoración, y por eso no
        la decide: `ExecuteBridge` valora con la cotización delante y se niega a
        cruzar cuando no puede medir. La línea existe para que ese «no se cruzará»
        no sorprenda, no para prometer nada.
        """
        if quote is None:
            return ()
        request = quote.request
        if bridge_notional(request) is not None:
            return ()
        return (
            f"el importe sale en {request.origin.symbol}, que no es la moneda de "
            f"los topes de {request.origin.chain}: se valorará al firmar, y si no "
            f"se puede valorar no se cruzará",
        )

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
        if spec.address_format is not AddressFormat.EVM_HEX:
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

    async def _resume_tracking(self) -> None:
        self._tracking.resume()

    def _on_tracked(self, record: TrackedBridge) -> None:
        dialogo = self._tx_dialogs.get(record.tx_hash)
        if dialogo is not None:
            dialogo.update_record(record)
        self._refresh_transactions()

    def _open_tracking(self, tx_hash: str) -> None:
        record = self._tracking.record(tx_hash)
        if record is None:
            return
        dialogo = self._tx_dialogs.get(tx_hash)
        if dialogo is None:
            dialogo = BridgeProgressDialog(record, self)
            self._tx_dialogs[tx_hash] = dialogo
        dialogo.update_record(record)
        dialogo.show()
        dialogo.raise_()

    def _refresh_transactions(self) -> None:
        registros = tuple(reversed(self._tracking.records()))
        self._tx.setRowCount(0)
        self._tx_hashes = []
        for record in registros:
            row = self._tx.rowCount()
            self._tx.insertRow(row)
            self._tx_hashes.append(record.tx_hash)
            emoji, texto, color = estado_visual(record)
            estado = QTableWidgetItem(f"{emoji}  {texto}")
            estado.setForeground(QColor(color))
            self._tx.setItem(row, 0, estado)
            self._tx.setItem(
                row, 1, QTableWidgetItem(f"{record.origin_chain} → {record.destination_chain}")
            )
            self._tx.setItem(row, 2, QTableWidgetItem(record.amount_text))
            self._tx.setCellWidget(
                row,
                3,
                _hash_cell(
                    enlace(record.origin_chain, record.tx_hash, texto=acortar(record.tx_hash)),
                    record.tx_hash,
                ),
            )
            if record.receiving_tx_hash:
                destino = _hash_cell(
                    enlace(
                        record.destination_chain,
                        record.receiving_tx_hash,
                        texto=acortar(record.receiving_tx_hash),
                    ),
                    record.receiving_tx_hash,
                )
            else:
                destino = _link_label("—")
            self._tx.setCellWidget(row, 4, destino)
            self._tx.setCellWidget(
                row,
                5,
                _hash_cell(
                    enlace_lifi(record.tx_hash, texto=acortar(record.tx_hash)),
                    record.tx_hash,
                ),
            )
            acciones = QWidget()
            fila_acciones = QHBoxLayout(acciones)
            fila_acciones.setContentsMargins(0, 0, 0, 0)
            detalle = QPushButton("Ver")
            detalle.setObjectName("rowAction")
            detalle.clicked.connect(
                lambda _checked=False, h=record.tx_hash: self._open_tracking(h)
            )
            fila_acciones.addWidget(detalle)
            if record.claimable:
                reclamar = QPushButton("Reclamar")
                reclamar.setObjectName("rowAction")
                reclamar.clicked.connect(
                    lambda _checked=False, h=record.tx_hash: spawn(self._tracking.claim(h))
                )
                fila_acciones.addWidget(reclamar)
            self._tx.setCellWidget(row, 6, acciones)
        set_empty(self._tx, self._tx_empty, "Aún no has emitido ningún puente.")

    def _on_execute(self) -> None:
        if self._executing:
            # Doble disparo (p. ej. doble clic antes de que Qt repinte): se
            # ignora sin más. No se encola nada.
            return
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
        self._executing = True
        self._exec_btn.setEnabled(False)
        self._status.setText("Firmando y emitiendo el cruce…")
        spawn(self._do_execute(quote, recipient))

    async def _do_execute(self, quote: BridgeQuote, recipient: str) -> None:
        try:
            receipt = await self._container.execute_bridge(quote, recipient=recipient)
            self._tracking.add(new_tracked_bridge(quote, receipt))
            self._open_tracking(receipt.tx_hash)
            self._status.setText(
                f"Emitida {receipt.tx_hash} · estado {receipt.status.value}. El dinero "
                f"salió de {quote.request.origin.chain}: el tiempo del cruce lo pone el "
                "proveedor, no la aplicación."
            )
            self._refresh_balances(forced=True)
        except ConfirmationDeniedError:
            self._status.setText("Cancelaste la operación. No se firmó ni se emitió nada.")
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._refresh_balances(forced=True)
        finally:
            self._executing = False
            self._on_route_selected()


def _link_label(texto: str) -> QLabel:
    etiqueta = QLabel(texto)
    etiqueta.setOpenExternalLinks(True)
    etiqueta.setContentsMargins(6, 0, 6, 0)
    return etiqueta


#: Las cifras se alinean a la derecha para que los decimales queden en columna.
_DERECHA: Final = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

#: Título corto y nombre completo de cada columna de la tabla de rutas. El título
#: corto es el que marca el ancho de la columna; el completo vive en el tooltip de
#: la cabecera, que es donde se puede leer sin robarle sitio a las cifras.
_COLUMNAS_RUTAS: Final[tuple[tuple[str, str], ...]] = (
    ("#", "Puesto de la ruta: 1 es la que más deja"),
    ("Puente", "Puente que mueve el dinero"),
    ("Motor", "Motor de Amigocompora que la cotiza"),
    ("Recibes", "Lo que llega al destino, ya neto de comisión"),
    ("Mínimo", "Mínimo garantizado que llega al destino"),
    ("Comisión", "Comisión del puente en puntos básicos"),
    ("Duración", "Lo que el proveedor estima que tarda"),
    ("Nota", "Lo que el proveedor cuenta de la ruta"),
)


def _cabeceras(tabla: QTableWidget, columnas: tuple[tuple[str, str], ...]) -> None:
    """Pone los títulos cortos y deja el nombre completo en el tooltip."""
    tabla.setHorizontalHeaderLabels([titulo for titulo, _ in columnas])
    for indice, (_, completo) in enumerate(columnas):
        cabecera = tabla.horizontalHeaderItem(indice)
        if cabecera is not None:
            cabecera.setToolTip(completo)


def _cifra(monto: TokenAmount, decimales: int = 6) -> str:
    """Cantidad con como mucho `decimales` cifras y sin ceros sobrantes."""
    minimo = Decimal(10) ** -decimales
    valor = monto.as_decimal()
    if valor != 0 and abs(valor) < minimo:
        return f"<{minimo:f} {monto.symbol}"
    texto = f"{valor.quantize(minimo, rounding=ROUND_HALF_EVEN):f}"
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return f"{texto} {monto.symbol}"


def _nota_corta(texto: str, limite: int = 60) -> str:
    """La nota recortada, para que la columna mida lo que mide el texto corto.

    Se recorta el texto y no el ancho de la columna: así cada celda sigue midiendo
    su contenido, sin anchos fijos, y la nota completa se lee en el tooltip.
    """
    if len(texto) <= limite:
        return texto
    return f"{texto[: limite - 1].rstrip()}…"


def _nota_item(texto: str) -> QTableWidgetItem:
    item = QTableWidgetItem(_nota_corta(texto))
    item.setToolTip(texto)
    return item


def _cifra_item(monto: TokenAmount) -> QTableWidgetItem:
    """La cantidad recortada en la celda, y la exacta en el tooltip."""
    item = QTableWidgetItem(_cifra(monto))
    item.setTextAlignment(_DERECHA)
    item.setToolTip(f"Cantidad exacta: {monto}")
    return item


def _hash_cell(enlace_html: str, tx_hash: str) -> QWidget:
    celda = QWidget()
    fila = QHBoxLayout(celda)
    fila.setContentsMargins(0, 0, 0, 0)
    fila.setSpacing(2)
    fila.addWidget(_link_label(enlace_html))
    copiar = QToolButton()
    copiar.setText("📋")
    copiar.setToolTip("Copiar el hash completo")
    copiar.setAutoRaise(True)
    copiar.clicked.connect(
        lambda _checked=False, h=tx_hash: QGuiApplication.clipboard().setText(h)
    )
    fila.addWidget(copiar)
    return celda


def _select_equivalent(combo: QComboBox, token: object) -> None:
    """Pone en el desplegable el token de **esta** red equivalente al dado.

    Se usa al invertir las patas, donde el token cambia de red: ahí no se puede
    buscar «el mismo token» —`is_same_asset` compara la red y nunca acertaría—,
    sino el que hace su papel en la red de enfrente. Se compara por el nombre con
    el que se enseña y no por el símbolo, porque en Polygon el símbolo no desempata
    el USDC nativo del puenteado; si ese nombre no está en la red nueva se prueba
    con el símbolo, que es lo que convierte un `USDC.e` de Polygon en el `USDC` de
    Base. Si tampoco está, se deja lo que eligió `_reload`: una pata con un token
    razonable es mejor que una pata apuntando a nada.
    """
    if not isinstance(token, Token):
        return
    for clave in (token.display_symbol, token.symbol):
        for index in range(combo.count()):
            candidato = combo.itemData(index)
            if isinstance(candidato, Token) and candidato.display_symbol == clave:
                combo.setCurrentIndex(index)
                return


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


_MOTOR_LABELS: dict[str, str] = {
    "lifi": "LI.FI",
    "relay": "Relay",
    "circle": "Circle (CCTP)",
}


def _motor_label(engine_id: str) -> str:
    return _MOTOR_LABELS.get(engine_id, engine_id)


def _cap_height(table: QTableWidget, filas: int = 8) -> None:
    """Le pone techo al alto de la tabla: el de sus filas, no el que le sobre.

    Es un **máximo**, no un alto fijo: por debajo encoge sola, que es lo que hace
    falta cuando la ventana es pequeña o cuando no hay filas con las que medir.
    """
    visible = min(table.rowCount(), filas)
    alto = (
        table.horizontalHeader().sizeHint().height()
        + table.horizontalScrollBar().sizeHint().height()
        + 2 * table.frameWidth()
    )
    for row in range(visible):
        alto += table.sizeHintForRow(row)
    table.setMaximumHeight(alto if visible else 0)
