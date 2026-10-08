"""Pestaña **Swap**: convertir una moneda en otra, y firmar si se puede.

La página tiene tres zonas, de arriba abajo, y el orden no es decorativo: es el
orden en que se toman las decisiones.

1. **La tarjeta de conversión.** Qué red, qué entregas y qué recibes, y cuánto.
   Se escribe en la unidad de lo que **entregas**, que es la única que el usuario
   conoce de antemano: lo que va a recibir es justo lo que viene a averiguar.
2. **Las rutas.** Lo que contestaron los motores, con la comisión y el impacto de
   cada uno, y la ruta elegida escrita en una línea.
3. **Lo que se puede hacer con ella.** Dos caminos de consecuencias opuestas y
   por eso dos botones distintos: *Preparar swap* construye el payload y lo
   confirma —no firma nada, y el resultado se puede guardar y firmar en otra
   cartera—, y *Ejecutar* firma y emite. Están separados —y no en uno solo con
   una casilla— porque la diferencia no es un grado: uno produce un fichero y el
   otro produce una transacción en la red. Un botón que hace una cosa u otra
   según cómo esté configurado algo convierte esa distinción en algo que hay que
   recordar en vez de algo que se ve.

### Sobre la dirección del par

El par se arma con lo que **entregas** como base y lo que **recibes** como
cotización, y la cantidad se escribe siempre en la base. Hubo antes un
desplegable «Comprar / Vender» que elegía qué pata era cuál: obligaba a traducir
mentalmente «vender WETH» a «WETH → USDC» en cada operación, y con esa traducción
repartida por la página la dirección acababa invertida en alguna vista y el
importe se firmaba al revés. Con dos desplegables —entregas y recibes— y un botón
para invertirlos, la operación ya está escrita en la pantalla tal como se firma.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.execute_swap import reference_notional
from amigocompora.domain.addresses import is_evm_address, is_solana_address
from amigocompora.domain.chains import CHAINS, AddressFormat, chain
from amigocompora.domain.errors import (
    ConfirmationDeniedError,
    ExecutionLimitExceededError,
)
from amigocompora.domain.models import (
    Opportunity,
    PlannedTransaction,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.wallet import ChainHoldings, WalletKind, WalletProfile
from amigocompora.engines.catalog import quote_token, token_by_address, wrapped_native
from amigocompora.ui.pages.bridges import BridgesSection
from amigocompora.ui.theme import (
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from amigocompora.ui.widgets import (
    Card,
    Chip,
    Field,
    format_amount,
    set_empty,
    spawn,
    token_labels,
    tokens_for_chain,
)

#: Aviso que acompaña a todo payload exportado. Va dentro del fichero y no sólo
#: en la pantalla: el fichero sobrevive a la sesión y viaja a otro sitio, y quien
#: lo abra tiene que saber qué tiene entre manos.
#:
#: Dice **de este documento** y no de la aplicación, que es la corrección que hizo
#: falta al añadir la ejecución: «Amigocompora no firma ni emite transacciones»
#: era verdad cuando este texto se escribió y dejó de serlo. Lo que sigue siendo
#: cierto pase lo que pase es que un fichero no firma nada por sí solo.
_UNSIGNED_NOTICE = (
    "Payload SIN FIRMAR: este documento no es una transacción emitida, y guardarlo "
    "no emite nada. Revísalo y fírmalo con tu propia cartera."
)

#: Lo que dice la tabla de rutas cuando aún no se ha pedido nada. Se escribe aquí
#: y no en el widget para que el mismo texto valga para el estado inicial y para
#: el que queda tras un error: los dos son «no hay rutas», y decir cosas distintas
#: en los dos sitios es la forma de que uno de los dos acabe mintiendo.
_SIN_COTIZAR = (
    "Todavía no has cotizado. Elige la red y las dos patas, y pulsa «Cotizar "
    "rutas»: cotizar no firma ni emite nada."
)


class PricesPage(QWidget):
    #: Un token nuevo —pegado por su dirección— que ya está en el almacén. La
    #: ventana lo lleva a la pestaña de cartera para que su lista de tokens
    #: vigilados lo incluya sin tener que releer la red.
    token_added = Signal(object)

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._comparison: PriceComparison | None = None
        self._prepared: PlannedTransaction | None = None
        #: Saldos leídos, por `(dirección, red)`. Una lectura de una red trae
        #: todos sus tokens, así que cambiar de pata dentro de la misma red no
        #: gasta una llamada de red más.
        self._balances: dict[tuple[str, str], ChainHoldings] = {}
        #: A quién pertenecen los saldos de la caché. Cuando la dirección que
        #: firma cambia —se guarda una credencial, se borra— la caché entera deja
        #: de ser de quien dice ser, y se tira.
        self._balance_owner: str = ""
        #: Lecturas de saldo **en vuelo**, por `(dirección, red)`.
        #:
        #: La caché de arriba sólo evita la segunda lectura **después** de que la
        #: primera haya llegado, y el hueco entre las dos es justo donde se
        #: repite: elegir la red repinta los dos desplegables, y cada repintado
        #: pide el saldo de la pata que se entrega en ese instante. Con la caché
        #: todavía vacía, las tres peticiones salen a la red y la misma lectura se
        #: hace tres veces —tres pasadas completas de nodos por un dato, y en
        #: Solana un nodo público racionado por ventana—.
        self._reading: set[tuple[str, str]] = set()

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        lay.addWidget(self._build_converter())
        # Las dos tablas miden lo que miden sus filas, así que las tarjetas se
        # quedan con su alto natural y el hueco sobrante —una ventana de 940 px
        # con dos rutas dentro— cae al final de la página en vez de repartirse
        # por dentro de cada tarjeta, que es donde antes aparecía como un
        # rectángulo gris debajo de los botones sin nada dentro.
        lay.addWidget(self._build_routes())
        lay.addWidget(self._build_opportunities())
        # Y por último cruzar de red, que es la operación más larga de las tres y
        # la que menos se hace: se elige igual —entregas, recibes, cuánto— así que
        # vive aquí y no en una pestaña aparte, pero va después de lo que se usa a
        # diario. Ver `bridges`.
        self._bridges = BridgesSection(container)
        lay.addWidget(self._bridges)
        # Con peso 1 y no 0: `addStretch()` sin argumento crea un espaciador con
        # peso **cero**, que no absorbe nada —Qt reparte el sobrante entre todos
        # los que pueden crecer, y a la tarjeta de rutas le tocaban ochenta
        # píxeles que no sabía usar mientras la de oportunidades se quedaba
        # cuarenta y ocho por debajo de su propio contenido. Con peso 1 el
        # sobrante tiene un único destinatario y las tarjetas se quedan con el
        # alto de lo que llevan dentro.
        lay.addStretch(1)

        self._table.itemSelectionChanged.connect(self._on_quote_selected)
        self._chain.currentIndexChanged.connect(self._refresh_tokens)
        self._base.currentIndexChanged.connect(self._update_legs_labels)
        self._contra.currentIndexChanged.connect(self._update_legs_labels)
        self._refresh_tokens()
        # El estado inicial de los botones y del chip de modo: sin esto la
        # tarjeta abre con el chip vacío hasta que alguien cotice, y el modo es
        # justo lo que hay que ver **antes** de ponerse a cotizar.
        self._on_quote_selected()
        self._clear_results(_SIN_COTIZAR)

    # ------------------------------------------------------------------ #
    # Zona 1 — la tarjeta de conversión
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hugs_content(card: Card) -> Card:
        """Una tarjeta que mide lo que mide su contenido, y ni un píxel más.

        Con la política por omisión una tarjeta *puede* crecer, y crecía: la de
        rutas acababa con ochenta píxeles de vacío entre los botones y su propio
        borde inferior, porque `QBoxLayout` reparte el sobrante entre todo lo que
        admita crecer aunque su contenido no llegue a llenarlo. `Maximum` no
        basta —el dato que usa el reparto es el peso, no la política—, así que
        se fija: el alto es el de su contenido, y el hueco que sobra se queda al
        final de la página, que es donde no molesta a nadie.
        """
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return card

    @staticmethod
    def _leg_box(caption: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        """Un recuadro con su título y una fila dentro: una pata del par.

        Devuelve el marco, el layout del recuadro y la fila donde van los
        controles, en vez de recibir los widgets ya hechos, porque las dos patas
        no llevan lo mismo —una escribe un importe y la otra lee un resultado— y
        forzarlas al mismo parámetro obligaría a inventar un widget vacío en la
        segunda. El layout del recuadro se devuelve para poder colgar una línea
        más **antes** del espaciador final, que es donde va el saldo disponible.
        """
        box = QFrame()
        box.setObjectName("legBox")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)
        title = QLabel(caption)
        title.setObjectName("sectionTitle")
        lay.addWidget(title)
        row = QHBoxLayout()
        row.setSpacing(8)
        lay.addLayout(row)
        lay.addStretch()
        return box, lay, row

    def _build_converter(self) -> QWidget:
        card = Card(
            "Convertir",
            subtitle="— eliges qué entregas y qué recibes; Amigocompora cotiza la ruta",
        )

        # El modo, a la vista en la propia tarjeta. No es un adorno: es lo que
        # decide si el botón de la derecha firma o no, y tenerlo aquí evita
        # subir a la barra a comprobarlo cada vez.
        self._mode_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._mode_chip)

        top = QHBoxLayout()
        self._chain = QComboBox()
        for key in sorted(CHAINS):
            self._chain.addItem(f"{CHAINS[key].name} ({key})", key)
        chain_field = Field("RED")
        chain_field.add(self._chain, 1)

        # Un token que no está en el catálogo entra por su dirección, y el
        # símbolo y los decimales se leen de su contrato. Es lo que hace que la
        # lista no sea el límite de lo que la aplicación sabe nombrar: con esto,
        # cualquier token de la red se puede cotizar y operar.
        self._lookup_btn = QPushButton("+ Token…")
        self._lookup_btn.setObjectName("secondary")
        self._lookup_btn.setToolTip(
            "Añade un token que no está en la lista, por su dirección de contrato. "
            "El símbolo y los decimales se leen de la cadena, no se piden a mano."
        )
        self._lookup_btn.clicked.connect(self._on_lookup_token)

        # Olvidar sólo tiene sentido para lo que se añadió a mano, y el botón
        # dice eso mismo cuando el token elegido viene del catálogo en vez de
        # desaparecer: un botón que se esconde deja al usuario sin saber si la
        # función existe.
        self._forget_btn = QPushButton("Olvidar token")
        self._forget_btn.setObjectName("secondary")
        self._forget_btn.setToolTip(
            "Quita de la lista un token que añadiste por su dirección. Los del "
            "catálogo no se pueden quitar: están en el código y volverían."
        )
        self._forget_btn.clicked.connect(self._on_forget_token)

        top.addWidget(chain_field)
        top.addStretch(1)
        top.addWidget(self._lookup_btn, 0, Qt.AlignBottom)
        top.addWidget(self._forget_btn, 0, Qt.AlignBottom)
        card.add_row(top)

        # Las dos patas, con el botón de invertir en medio. La cantidad va en la
        # pata que se entrega, pegada a su unidad: sin la unidad el mismo «1» es
        # un token o mil dólares, y esto acaba en una firma.
        #
        # Cada pata va en su propio recuadro, y los dos del mismo tamaño. Antes
        # eran dos filas de campos sueltas y la de «recibes» quedaba más corta
        # —no tiene importe que escribir, sólo uno que leer—, así que la mitad
        # derecha de la tarjeta parecía a medio construir y el ojo no emparejaba
        # las dos mitades de lo que es, literalmente, un intercambio.
        legs = QHBoxLayout()
        legs.setSpacing(10)

        self._base = QComboBox()
        self._amount = QDoubleSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        self._amount.setMinimumWidth(150)
        self._unit = QLabel("")
        self._unit.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 700;")

        give_box, give_lay, give = self._leg_box("ENTREGAS")
        give.addWidget(self._base, 1)
        give.addWidget(self._amount)
        give.addWidget(self._unit)

        # El saldo disponible de lo que se entrega, con su botón de «Máx».
        #
        # Va pegado al importe porque es la pregunta que se hace justo antes de
        # escribirlo: «¿cuánto tengo de esto?». Sin esta línea hay que irse a la
        # pestaña de la cartera, buscar el token, volver y teclear de memoria una
        # cifra que se firma — y de ahí salen los importes redondos que no caben.
        #
        # Se lee de la **misma** cartera que firma y no de una copia: el saldo que
        # se enseña aquí es el que la política y el caso de uso van a comprobar
        # antes de emitir, así que no puede discrepar de ellos.
        self._balance = QLabel("")
        self._balance.setObjectName("hint")
        self._balance.setWordWrap(True)
        self._max_btn = QPushButton("Máx")
        self._max_btn.setObjectName("secondary")
        self._max_btn.setToolTip(
            "Pone el importe entero del saldo disponible. El campo redondea a seis "
            "decimales, así que para un token de dieciocho cifras deja fuera lo "
            "que no cabe en seis —siempre por debajo del saldo, nunca por encima."
        )
        self._max_btn.clicked.connect(self._on_max)
        saldo_row = QHBoxLayout()
        saldo_row.setContentsMargins(0, 0, 0, 0)
        saldo_row.setSpacing(8)
        saldo_row.addWidget(self._balance, 1)
        saldo_row.addWidget(self._max_btn)
        give_lay.insertLayout(2, saldo_row)

        # Con texto y no con el glifo «⇅»: dibujado con la fuente del sistema
        # sale un signo diminuto que se lee como una letra suelta, y este es el
        # único control de la tarjeta que no se entiende por lo que tiene al
        # lado. Una palabra no depende de qué fuente haya instalada.
        self._invert_btn = QPushButton("Invertir")
        self._invert_btn.setObjectName("secondary")
        self._invert_btn.setToolTip(
            "Invertir el par: pasa lo que entregas a lo que recibes y cotiza la "
            "operación contraria."
        )
        self._invert_btn.clicked.connect(self._on_invert)

        self._contra = QComboBox()
        # El resultado de la cotización, en grande: es la cifra que el usuario
        # venía a averiguar, y estaba en el mismo gris y el mismo tamaño que una
        # etiqueta cualquiera. Alineado a la derecha para que caiga justo debajo
        # del importe que lo produce, que es su sitio natural en la lectura.
        self._out_estimate = QLabel("—")
        self._out_estimate.setObjectName("bigNumber")
        self._out_estimate.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._out_estimate.setStyleSheet(f"color: {COLOR_MUTED};")
        self._out_estimate.setMinimumWidth(150)
        self._out_estimate.setToolTip(
            "Lo que devolvería la mejor ruta. Se rellena al cotizar."
        )

        receive_box, _, receive = self._leg_box("RECIBES")
        receive.addWidget(self._contra, 1)
        receive.addWidget(self._out_estimate)

        legs.addWidget(give_box, 1)
        legs.addWidget(self._invert_btn, 0, Qt.AlignCenter)
        legs.addWidget(receive_box, 1)
        card.add_row(legs)

        bottom = QHBoxLayout()
        self._btn = QPushButton("Cotizar rutas")
        self._btn.clicked.connect(self._on_quote)
        bottom.addWidget(self._btn)
        self._pair = QLabel("—")
        self._pair.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 600;")
        bottom.addWidget(self._pair)
        bottom.addStretch()
        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        bottom.addWidget(self._status, 2)
        card.add_row(bottom)
        return self._hugs_content(card)

    # ------------------------------------------------------------------ #
    # Zona 2 — las rutas y lo que se puede hacer con ellas
    # ------------------------------------------------------------------ #
    def _build_routes(self) -> QWidget:
        card = Card("Rutas encontradas")
        self._route_count = QLabel("")
        self._route_count.setObjectName("hint")
        card.header.addWidget(self._route_count)

        # Una tabla sin filas es un rectángulo gris grande que no dice nada: no
        # se distingue «todavía no has pedido nada» de «pediste y no hay ruta».
        # El rótulo ocupa el sitio de la tabla mientras está vacía y lo explica.
        self._routes_empty = QLabel("")
        self._routes_empty.setObjectName("empty")
        self._routes_empty.setWordWrap(True)
        self._routes_empty.setAlignment(Qt.AlignCenter)
        card.body().addWidget(self._routes_empty, stretch=1)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Venue", "Motor", "Recibes", "Precio", "Comisión", "Impacto", "Liquidez", "Nota"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        # Una fila por ruta y una línea por fila, con el texto largo recortado:
        # es una tabla para comparar de un vistazo, no para leer prosa. Lo que no
        # cabe se recupera en el tooltip de cada celda.
        self._table.setWordWrap(False)
        card.body().addWidget(self._table, stretch=1)

        # La ruta elegida, escrita. Entre la tabla y los botones, porque es lo
        # que los botones van a usar: sin esta línea, «Ejecutar» actúa sobre una
        # fila resaltada en una tabla de cifras y hay que deducir cuál.
        self._chosen = QLabel("Selecciona una ruta para prepararla o firmarla.")
        self._chosen.setObjectName("hint")
        # De una línea, aunque el texto sea largo. Una `QLabel` con `wordWrap`
        # publica un alto natural calculado para un ancho cualquiera —varias
        # líneas— y luego se dibuja en una: esa diferencia quedaba como un hueco
        # muerto al final de la tarjeta, entre los botones y el borde. El texto
        # entero, por si se recorta, va en el tooltip.
        self._chosen.setWordWrap(False)
        card.body().addWidget(self._chosen)

        actions = QHBoxLayout()
        self._swap_btn = QPushButton("Preparar swap…")
        self._swap_btn.setObjectName("secondary")
        self._swap_btn.setToolTip(
            "Construye la transacción sin firmar de la ruta seleccionada. "
            "Este botón no firma ni emite nada: para eso está el de al lado, que "
            "es irreversible."
        )
        self._swap_btn.clicked.connect(self._on_prepare_swap)
        self._swap_btn.setEnabled(False)
        actions.addWidget(self._swap_btn)

        self._exec_btn = QPushButton("Ejecutar: firmar y emitir…")
        self._exec_btn.setObjectName("danger")
        self._exec_btn.setToolTip(
            "Firma la transacción y la emite a la red. Es irreversible y sale "
            "dinero de tu cartera."
        )
        self._exec_btn.clicked.connect(self._on_execute)
        self._exec_btn.setEnabled(False)
        actions.addWidget(self._exec_btn)

        self._save_btn = QPushButton("Guardar payload…")
        self._save_btn.setObjectName("secondary")
        self._save_btn.setToolTip("Exporta a JSON la última transacción sin firmar que confirmaste.")
        self._save_btn.clicked.connect(self._on_save_payload)
        self._save_btn.setEnabled(False)
        actions.addWidget(self._save_btn)
        actions.addStretch()
        card.add_row(actions)

        # Por qué «Ejecutar» está apagado, en la misma línea y no escondido en un
        # tooltip. Un botón apagado sin motivo es lo que empuja a buscar la forma
        # de saltárselo, así que aquí se dice qué falta exactamente — y se dice
        # **antes** de que alguien lo intente, no cuando falla.
        #
        # Y cuando lo que falta es el modo, al lado va el atajo para cambiarlo:
        # el mensaje decía «cambia a EJECUCIÓN» y dejaba al usuario buscando
        # dónde. El modo sólo lo cambia una acción explícita del usuario, y un
        # botón que se pulsa lo es; lo que no puede haber es que lo cambie el
        # programa por su cuenta.
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
        return self._hugs_content(card)

    def _build_opportunities(self) -> QWidget:
        card = Card("Oportunidades", subtitle="— diferencial neto ≥ 10 bps")
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
        return self._hugs_content(card)

    # ------------------------------------------------------------------ #
    # Filtros
    # ------------------------------------------------------------------ #
    def _available_tokens(self, chain_key: str) -> tuple[Token, ...]:
        """Los tokens que se pueden elegir en esta red: catálogo más añadidos.

        Sin quitar la stablecoin de referencia, que es lo que hacía la versión
        anterior: mientras el par era siempre «token contra stablecoin», ofrecer
        la stablecoin como token era ofrecer un par consigo misma. En cuanto la
        otra pata se elige aparte, comprar dólares con ETH es una operación
        perfectamente normal y el filtro sobraba.

        La regla —catálogo más lo guardado, sin repetir— vive en `widgets` porque
        la tarjeta de puentes la usa igual: un token añadido por su contrato tiene
        que aparecer en las dos.
        """
        return tokens_for_chain(self._container.token_store, chain_key)

    def _refresh_tokens(self) -> None:
        key = self._chain.currentData()
        entrega_previa = self._base.currentData()
        recibe_previa = self._contra.currentData()
        self._base.clear()
        self._contra.clear()
        tokens = self._available_tokens(key)
        for etiqueta, token in zip(token_labels(tokens), tokens, strict=True):
            self._base.addItem(etiqueta, token)
            self._contra.addItem(etiqueta, token)
        self._mark_unusable_tokens()

        # Las dos patas se conservan si siguen existiendo en esta red, y si no se
        # cae a las dos monedas con las que de verdad hay mercado: el envoltorio
        # nativo —que es con lo que operan los AMM— para lo que se entrega, y la
        # stablecoin de referencia para lo que se recibe, que es la unidad en la
        # que se comparan los precios de todos los venues.
        #
        # Conservarlas importa porque esta función también se llama al añadir un
        # token por dirección: repintar las listas no es motivo para deshacer una
        # elección que el usuario acaba de hacer.
        entrega = _index_of(self._base, entrega_previa)
        if entrega < 0:
            entrega = _index_of(self._base, wrapped_native(key))
        self._base.setCurrentIndex(max(entrega, 0))

        recibe = _index_of(self._contra, recibe_previa)
        if recibe < 0:
            recibe = _index_of(self._contra, quote_token(key) or wrapped_native(key))
        if recibe >= 0:
            self._contra.setCurrentIndex(recibe)
        self._update_legs_labels()

    def _mark_unusable_tokens(self) -> None:
        """Marca los tokens con los que la política no dejaría firmar.

        Se marca aquí y no al pulsar «Ejecutar» porque descubrirlo al final es
        descubrirlo tarde: el usuario habría cotizado, elegido ruta y llegado
        hasta el diálogo para que le dijeran que ese token no está en la lista
        blanca. El desplegable sabe qué se puede ejecutar desde el primer momento,
        así que lo dice desde el primer momento.

        Se pinta el **texto** en gris y se explica en el tooltip, sin tocar la
        etiqueta: la etiqueta identifica el token y hay código que la busca por
        texto, así que añadirle un sufijo rompería una búsqueda para arreglar un
        aviso que cabe en el color.

        Con la lista blanca vacía no se marca nada: vacía significa «no hay nada
        permitido», y pintar de gris las once entradas de las nueve redes
        describiría una aplicación rota en vez de una a medio configurar. De eso
        ya avisa la lista de motivos, que es donde se dice entera.
        """
        permitidos = self._container.policy.limits.allowed_tokens
        if not permitidos:
            return
        for combo in (self._base, self._contra):
            for index in range(combo.count()):
                token = combo.itemData(index)
                if not isinstance(token, Token) or token.symbol in permitidos:
                    continue
                combo.setItemData(index, QColor(COLOR_MUTED), Qt.ItemDataRole.ForegroundRole)
                combo.setItemData(
                    index,
                    f"«{token.symbol}» no está en `allowed_tokens`, así que la "
                    "política no dejará firmar una operación que lo toque. Se "
                    "puede cotizar igual: cotizar no mueve dinero.",
                    Qt.ItemDataRole.ToolTipRole,
                )

    def _on_lookup_token(self) -> None:
        """Añade un token por su dirección, leyendo su contrato."""
        chain_key: str = self._chain.currentData()
        address, accepted = QInputDialog.getText(
            self,
            "Token por dirección",
            f"Dirección del contrato en {CHAINS[chain_key].name}:\n"
            "(el símbolo y los decimales se leen de la cadena)",
        )
        if not accepted or not address.strip():
            return
        self._lookup_btn.setEnabled(False)
        self._status.setText("Leyendo el contrato del token…")
        spawn(self._do_lookup_token(chain_key, address.strip()))

    async def _do_lookup_token(self, chain_key: str, address: str) -> None:
        try:
            token = await self._container.token_lookup.by_address(chain_key, address)
        except Exception as error:
            self._status.setText(f"No se pudo añadir el token: {error}")
            return
        finally:
            self._lookup_btn.setEnabled(True)

        # Ya estaba en el almacén —o en el catálogo—: se refresca igual, porque
        # lo que importa es que quede seleccionado y visible, no de dónde salió.
        #
        # Y se selecciona por **identidad**, no por el texto de la etiqueta: dos
        # tokens de la misma red pueden publicar el mismo símbolo —en Polygon lo
        # hacen los dos USDC— y ahí la etiqueta lleva la dirección pegada, así que
        # buscar por texto no encontraría el que se acaba de añadir.
        self._container.token_store.add(token)
        self._refresh_tokens()
        indice = _index_of(self._base, token)
        if indice >= 0:
            self._base.setCurrentIndex(indice)
        # Y se avisa a quien tenga la lista de tokens vigilados: la pestaña de
        # cartera tiene la suya, y un token que se añade aquí y no aparece allí
        # sería la misma función dando dos respuestas.
        self.token_added.emit(token)
        self._status.setText(
            f"Añadido {token.symbol} ({token.decimals} decimales) desde {token.address}. "
            f"Ya se puede cotizar, y queda guardado para la próxima vez."
        )

    def prepare_with_token(self, token: Token) -> None:
        """Deja el par listo para operar con un token, viniendo de otra pestaña.

        Es lo que hace que «intercambiar este token» desde la cartera signifique
        algo: pone la **red** del token, lo elige como lo que se entrega y deja la
        pata de enfrente en la stablecoin de esa red, que es la moneda con la que
        se compara todo lo demás. Público porque quien conoce el token es la
        pestaña de cartera y quien sabe cambiarse de pestaña es la ventana: un
        método privado de otra clase no es una interfaz, es un atajo.

        No cotiza sola. Cotizar es una llamada a la red por cada motor activo, y
        hacerlo al llegar —cuando el usuario todavía no ha escrito cuánto quiere
        entregar— sería gastar seis llamadas para contestar una pregunta que aún
        no se ha hecho.
        """
        indice_red = -1
        for indice in range(self._chain.count()):
            if self._chain.itemData(indice) == token.chain:
                indice_red = indice
                break
        if indice_red < 0:
            self._status.setText(
                f"La red «{token.chain}» no está en la lista de la pestaña de swap."
            )
            return

        # Cambiar de red reconstruye las dos listas de tokens, así que va primero.
        if self._chain.currentIndex() != indice_red:
            self._chain.setCurrentIndex(indice_red)

        # Y si el token no estuviera en la lista —el caso de un token que se ve en
        # la cartera porque lo leyó un nodo, pero que nadie ha añadido—, se añade
        # aquí, que es lo que hace que el botón no pueda fallar en silencio.
        #
        # El nativo no necesita nada de esto: `tokens_for_chain` lo pone primero en
        # todas las redes, así que ETH y POL ya están en la lista antes de que
        # nadie los pida. Y no se guarda aunque se pida: un token sin dirección no
        # tiene contrato que pegar, y el almacén lo rechaza diciéndolo.
        if _index_of(self._base, token) < 0:
            self._container.token_store.add(token)
            self._refresh_tokens()

        indice = _index_of(self._base, token)
        if indice < 0:
            self._status.setText(f"No se pudo poner «{token.symbol}» en la pata de entrega.")
            return
        self._base.setCurrentIndex(indice)

        # La otra pata, contra la stablecoin de la red. No se pisa si ya era la
        # stablecoin: `_refresh_tokens` la conserva entre redes cuando existe, y
        # volver a ponerla aquí no cambiaría nada.
        referencia = quote_token(token.chain)
        if referencia is not None and not referencia.is_same_asset(token):
            indice_contra = _index_of(self._contra, referencia)
            if indice_contra >= 0:
                self._contra.setCurrentIndex(indice_contra)

        self._update_legs_labels()
        self._status.setText(
            f"Listo para convertir {token.symbol} en {CHAINS[token.chain].name}. "
            f"Escribe el importe y pulsa «Cotizar rutas»."
        )

    def _on_forget_token(self) -> None:
        """Quita de la lista un token añadido a mano.

        Sólo se pueden olvidar los añadidos: los del catálogo están en el código
        y volverían a aparecer en el siguiente repintado, así que ofrecer el botón
        para ellos sería un botón que no hace nada.

        Quién es «añadido» lo dice el catálogo, no una lista paralela en memoria.
        La había —una copia de lo que se iba guardando— y era una tercera versión
        de la misma verdad: bastaba con que se desviara para que un token del
        catálogo se pudiera olvidar o uno propio no. El catálogo y el almacén son
        las dos fuentes que existen, y son las que se preguntan.
        """
        token = self._base.currentData()
        if not isinstance(token, Token):
            return
        if token.address is None:
            self._status.setText(
                "La moneda de la red no se puede olvidar: no se añadió a mano, la "
                "pone la propia red y se lee siempre."
            )
            return
        if token_by_address(token.address, token.chain) is not None:
            self._status.setText(
                f"«{token.symbol}» lo trae el catálogo, no lo añadiste tú: no hay "
                f"nada que olvidar."
            )
            return
        self._container.token_store.forget(token.chain, token.address)
        self._refresh_tokens()
        self._status.setText(
            f"Olvidado {token.symbol} ({token.address}). Vuelve a añadirlo pegando "
            f"su dirección si lo necesitas."
        )

    def _update_legs_labels(self) -> None:
        """Qué se entrega y qué se recibe, y en qué unidad va la cantidad.

        La cantidad se escribe en la unidad de la pata que **se entrega**, y esa
        pata la elige un desplegable que está tres widgets más allá. Por eso la
        etiqueta no es decoración: sin ella el mismo número significa dos cosas
        distintas, y esto acaba en una firma.
        """
        entrega, recibe = self._legs()
        if entrega is None or recibe is None:
            self._pair.setText("—")
            self._unit.setText("")
            return
        self._pair.setText(f"{entrega.symbol} → {recibe.symbol}")
        self._unit.setText(entrega.symbol)
        # La estimación es de la ruta anterior: en cuanto cambia el par deja de
        # valer, y una cifra vieja junto a un par nuevo es una cifra falsa.
        self._out_estimate.setText("—")
        # El saldo depende de qué se entrega, así que se refresca donde se
        # refresca la unidad. Puesto en un solo sitio, no hay forma de cambiar la
        # pata y quedarse mirando el saldo del token anterior —que es un error que
        # se comete solo, porque la cifra sigue ahí y parece válida.
        self._refresh_balance()

    # ------------------------------------------------------------------ #
    # El saldo disponible de lo que se entrega
    # ------------------------------------------------------------------ #
    def _owner(self) -> str:
        """La dirección de la cartera que firma, o cadena vacía si no hay clave."""
        return self._container.keys.address() or ""

    def _refresh_balance(self) -> None:
        """Enseña el saldo de lo que se entrega, leyéndolo si no está en la caché.

        La caché es por `(dirección, red)` y no por token: **una lectura de una
        red devuelve todos sus saldos de golpe**, así que cambiar de token dentro
        de la misma red no es motivo para volver a preguntar al nodo. Y se vacía
        entera en cuanto cambia la dirección, porque un saldo de otra cartera es
        la peor clase de dato viejo: el mismo número, atribuido a quien no es.
        """
        dueno = self._owner()
        if dueno != self._balance_owner:
            self._balances.clear()
            self._balance_owner = dueno

        entrega, _ = self._legs()
        cadena = self._chain.currentData()

        if not dueno or entrega is None or not isinstance(cadena, str):
            self._balance.setText("")
            self._max_btn.setEnabled(False)
            self._max_btn.setToolTip(
                "No hay ninguna cartera configurada: pon la clave privada en la "
                "pestaña de Motores, en Credenciales, y aquí aparecerá el saldo "
                "disponible de lo que entregas."
            )
            return

        if entrega.chain != cadena:
            # El token y la red no concuerdan —pasa en el instante en que el
            # desplegable de red ya cambió y el de tokens todavía no—. Se calla en
            # vez de enseñar el saldo de otra red, que sería un número cierto de
            # un sitio equivocado.
            self._balance.setText("")
            self._max_btn.setEnabled(False)
            return

        leido = self._balances.get((dueno, cadena))
        if leido is not None:
            self._paint_balance(leido, entrega)
            return

        self._balance.setText("Leyendo el saldo…")
        self._max_btn.setEnabled(False)
        # Una lectura que ya está en vuelo no se repite: la que está en camino
        # pintará el saldo cuando llegue, y lo pintará para el token que se
        # entregue **entonces** y no para el que se entregaba cuando se pidió.
        # Pedirla otra vez por un repintado no adelanta nada y multiplica las
        # llamadas al nodo.
        clave = (dueno, cadena)
        if clave in self._reading:
            return
        self._reading.add(clave)
        spawn(self._do_read_balance(dueno, cadena))

    async def _do_read_balance(self, address: str, chain_key: str) -> None:
        """Suelta la marca de «en vuelo», pase lo que pase.

        La marca se suelta en un `finally` y no al final del camino bueno. Si se
        quedara puesta al fallar, un corte de red se convertiría en un saldo que
        ya no se puede volver a pedir hasta reiniciar la aplicación, que es
        exactamente lo contrario de lo que hace falta justo después de un fallo.
        """
        try:
            await self._read_balance(address, chain_key)
        finally:
            self._reading.discard((address, chain_key))

    async def _read_balance(self, address: str, chain_key: str) -> None:
        """Lee una red entera y se queda con sus saldos.

        Se pide **sólo esa red**, que es lo que hace que la tarjeta de conversión
        no dispare nueve lecturas por un saldo. El motor devuelve el fallo dentro
        del objeto en vez de lanzar, así que aquí no se envuelve en un `except`
        que lo esconda: una red que no contesta tiene que llegar a la pantalla con
        su motivo, como en la cartera.
        """
        perfil = WalletProfile(
            wallet_id="principal",
            label="Mi cartera",
            kind=WalletKind.of_chain(chain_key),
            address=address,
        )
        try:
            snapshot = await self._container.read_wallet(perfil, chains=(chain_key,))
        except Exception as error:
            # Se enseña y se sale. Un fallo al leer un saldo no puede tumbar la
            # tarjeta de conversión: lo único que falta es un dato, y decirlo es
            # más útil que propagar la excepción hasta un manejador que la
            # resumiría en «error».
            self._paint_balance_error(str(error))
            return

        for cadena in snapshot.chains:
            if cadena.chain == chain_key:
                self._balances[(address, chain_key)] = cadena
                break

        entrega, _ = self._legs()
        if entrega is not None and entrega.chain == chain_key:
            self._paint_balance(self._balances[(address, chain_key)], entrega)

    def _paint_balance_error(self, motivo: str) -> None:
        self._balance.setText(f"No se pudo leer el saldo: {motivo}")
        self._balance.setStyleSheet(f"color: {COLOR_WARNING};")
        self._max_btn.setEnabled(False)

    def _paint_balance(self, cadena: ChainHoldings, token: Token) -> None:
        """El saldo del token que se entrega en la red que se está mirando.

        Los tres desenlaces —leído, a cero, sin poder leer— se dicen con
        palabras distintas. «0» y «no se pudo leer» son dos cosas muy distintas y
        el mismo guion en la pantalla haría que un fallo de red pareciera una
        cartera vacía, que es la conclusión que lleva a no operar sin motivo.
        """
        if cadena.failed:
            self._paint_balance_error(cadena.error or "la red no contestó")
            return

        holding = next(
            (h for h in cadena.holdings if h.token.is_same_asset(token)),
            None,
        )
        if holding is None:
            self._balance.setText(
                f"Saldo en {CHAINS[cadena.chain].name}: sin datos de {token.symbol} "
                f"en esta lectura."
            )
            self._balance.setStyleSheet(f"color: {COLOR_MUTED};")
            self._max_btn.setEnabled(False)
            return

        cantidad = format_amount(holding.as_decimal())
        self._balance.setText(f"Disponible: {cantidad} {token.symbol}")
        self._balance.setStyleSheet("")
        # El tooltip lleva la cifra entera: el recorte es de la pantalla y tener
        # que irse a la cartera para comprobar un saldo sería esconder un dato ya
        # calculado.
        pistas = [f"{holding.as_decimal():f} {token.symbol}"]
        if cadena.missing:
            pistas.append("Falta: " + "; ".join(cadena.missing))
        if token.is_native:
            # El aviso de siempre, en el sitio donde se comete el error: el gas
            # de la operación sale del mismo saldo que se está intentando mover.
            pistas.append(
                "Es la moneda de la red, así que la comisión de la operación sale "
                "de este mismo saldo: entregarlo entero no cabe."
            )
        self._balance.setToolTip("\n".join(pistas))
        self._max_btn.setEnabled(holding.amount.raw > 0)
        if holding.amount.raw == 0:
            self._max_btn.setToolTip(f"No hay saldo de {token.symbol} en esta red.")

    def _on_max(self) -> None:
        """Pone el importe entero, y dice lo que el campo no puede escribir.

        `QDoubleSpinBox` tiene seis decimales y el saldo puede tener dieciocho, así
        que el campo escribe **menos** de lo que hay —nunca más, que es la
        dirección segura del redondeo—. Se dice cuánto queda fuera en vez de
        callarlo: un «Máx» que deja 0,000000000001 ETH sin poner y no lo dice es
        un botón que miente sobre lo que hace.
        """
        entrega, _ = self._legs()
        cadena = self._chain.currentData()
        if entrega is None or not isinstance(cadena, str):
            return
        leido = self._balances.get((self._owner(), cadena))
        if leido is None or leido.failed:
            return
        holding = next(
            (h for h in leido.holdings if h.token.is_same_asset(entrega)),
            None,
        )
        if holding is None or holding.amount.raw == 0:
            return

        completo = holding.as_decimal()
        self._amount.setValue(float(completo))
        escrito = Decimal(str(self._amount.value()))
        if escrito < completo:
            self._status.setText(
                f"Máx: {format_amount(escrito, full=True)} de "
                f"{format_amount(completo, full=True)} {entrega.symbol} — el campo "
                f"tiene seis decimales y no cabe el resto."
            )
        else:
            self._status.setText("")

    def _on_invert(self) -> None:
        """Da la vuelta al par, conservando lo elegido en cada pata."""
        entrega = self._base.currentData()
        recibe = self._contra.currentData()
        if entrega is None or recibe is None:
            return
        indice_recibe = _index_of(self._base, recibe)
        indice_entrega = _index_of(self._contra, entrega)
        if indice_recibe < 0 or indice_entrega < 0:
            return
        self._base.setCurrentIndex(indice_recibe)
        self._contra.setCurrentIndex(indice_entrega)
        self._update_legs_labels()

    def _legs(self) -> tuple[Token | None, Token | None]:
        """Las dos patas del par, en el orden en que se mueven.

        `entrega` es lo que sale de la cartera y `recibe` lo que entra. El par se
        arma con ellas y **no se vuelve a consultar la dirección en ningún sitio**
        de los de después: si esta traducción estuviera repartida por la página,
        el importe acabaría firmado al revés en alguna de las vistas.
        """
        entrega = self._base.currentData()
        recibe = self._contra.currentData()
        if not isinstance(entrega, Token) or not isinstance(recibe, Token):
            return None, None
        return entrega, recibe

    # ------------------------------------------------------------------ #
    # Cotizar
    # ------------------------------------------------------------------ #
    def _on_quote(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando…")
        spawn(self._do_quote())

    async def _do_quote(self) -> None:
        try:
            entrega, recibe = self._legs()
            if entrega is None or recibe is None:
                self._status.setText("Elige el token y la moneda contra la que cotizarlo.")
                self._clear_results("Elige las dos patas del par para poder cotizar.")
                return
            if entrega.is_same_asset(recibe):
                # Se dice en vez de impedirlo: los dos desplegables son libres a
                # propósito —bloquear combinaciones entre ellos convierte un
                # error evidente en un desplegable que se mueve solo y no se
                # entiende—, y la etiqueta del par ya lo está enseñando.
                self._status.setText(
                    f"«{entrega.symbol}» en las dos patas no es una operación: "
                    f"elige dos tokens distintos."
                )
                self._clear_results(
                    f"«{entrega.symbol}» en las dos patas no es una operación: "
                    "elige dos tokens distintos."
                )
                return

            # Qué lado es `base` **es** la dirección del par: `base` es lo que se
            # entrega. Y la cantidad se expresa siempre en el `base`, así que con
            # el par bien armado la dirección no vuelve a aparecer en ninguna
            # cuenta de las de después.
            pair = TradingPair(base=entrega, quote=recibe)
            amount = pair.base.amount(Decimal(str(self._amount.value())))
            comparison = await self._container.compare_prices(pair, amount)
            self._comparison = comparison
            self._fill_table(comparison)
            # La mejor ruta queda elegida. Obligar a pulsar una fila después de
            # haber pedido la comparación es pedir dos veces lo mismo, y deja los
            # botones apagados sin que se vea por qué.
            if self._table.rowCount() > 0:
                self._table.selectRow(0)
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
                f"{len(comparison.quotes)} venue(s) · spread {comparison.spread_bps} "
                f"· {note} · observado {comparison.observed_at.isoformat()}"
            )
        except Exception as error:
            self._status.setText(f"Error: {error}")
            self._clear_results(f"No se pudo cotizar: {error}")
        finally:
            self._btn.setEnabled(True)
            self._on_quote_selected()

    def _fill_table(self, comp: PriceComparison) -> None:
        self._table.setRowCount(0)
        self._route_count.setText(f"{len(comp.quotes)} ruta(s) de {comp.pair.base.symbol}")
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
            out = QTableWidgetItem(str(quote.amount_out))
            if row == 0:
                # La primera fila es la mejor ruta: se marca en verde para que se
                # lea sin comparar las cifras una a una.
                out.setForeground(QColor(COLOR_SUCCESS))
            self._table.setItem(row, 2, out)
            self._table.setItem(row, 3, QTableWidgetItem(str(quote.price)))
            item_fee = QTableWidgetItem(f"{fee} [{fee_basis}]")
            if not quote.fee_is_known:
                item_fee.setForeground(QColor(COLOR_WARNING))
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
                item_imp.setForeground(QColor(COLOR_WARNING))
            self._table.setItem(row, 5, item_imp)
            self._table.setItem(
                row, 6, QTableWidgetItem(str(quote.liquidity) if quote.liquidity else "—")
            )
            nota = QTableWidgetItem(quote.source_note)
            # La nota se recorta en pantalla —la fila tiene que seguir siendo de
            # una línea— así que el texto entero se lee aquí. Sin esto, la
            # columna que dice de dónde sale cada cifra sería la única que no se
            # puede leer completa.
            nota.setToolTip(quote.source_note)
            self._table.setItem(row, 7, nota)
        set_empty(
            self._table,
            self._routes_empty,
            "Ningún motor activo devolvió una ruta para este par y este importe. "
            "Cotizar no firma nada: se puede probar con otro importe o con otro par.",
        )
        _cap_height(self._table)

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
        set_empty(
            self._opp_table,
            self._opps_empty,
            "Ningún diferencial supera el umbral para este importe: los venues "
            "coinciden dentro de lo que cuesta mover el dinero de uno a otro.",
        )
        _cap_height(self._opp_table)

    def _clear_results(self, motivo: str) -> None:
        """Deja las dos tablas vacías y **diciendo por qué** están vacías."""
        self._table.setRowCount(0)
        self._opp_table.setRowCount(0)
        self._route_count.setText("")
        set_empty(self._table, self._routes_empty, motivo)
        set_empty(self._opp_table, self._opps_empty, motivo)
        _cap_height(self._table)
        _cap_height(self._opp_table)

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
        quote = self._selected_quote()
        self._swap_btn.setEnabled(
            quote is not None and self._container.prepare_swap.is_available()
        )
        motivos = self._execution_blockers(
            quote.pair if quote is not None else None, quote
        )
        self._exec_btn.setEnabled(quote is not None and not motivos)

        if quote is None:
            # Con la tabla vacía el rótulo que ocupa su sitio ya dice qué hacer;
            # repetir aquí lo mismo sería la misma frase dos veces seguidas en la
            # misma tarjeta. La línea existe para cuando **sí** hay rutas y
            # ninguna elegida, que es cuando hace falta decir qué falta.
            self._chosen.setText(
                "Selecciona una ruta para prepararla o firmarla."
                if self._table.rowCount() > 0
                else ""
            )
            self._chosen.setToolTip("")
            self._out_estimate.setText("—")
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
            self._out_estimate.setText(f"≈ {quote.amount_out}")

        # El motivo se enseña sólo cuando ya hay una cotización elegida: antes de
        # eso el botón apagado no dice nada que el usuario no sepa ya, y un aviso
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
        desbloquearía nada, y un botón que no arregla lo que promete es peor que
        no tenerlo.
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
        color = COLOR_SUCCESS if self._container.guard.allows(Capability.BROADCAST_TX) else COLOR_MUTED
        self._mode_chip.set_state(f"MODO {mode.label}", color)
        self._mode_chip.setToolTip(f"{mode.label}: {mode.description}")

    def _on_upgrade_mode(self) -> None:
        """Sube al modo que concede firmar y emitir, por acción explícita.

        `ModeGuard.set_mode` está documentado como algo que **sólo** puede venir
        de una acción del usuario: ni un caso de uso ni un motor pueden subir de
        modo para desbloquearse a sí mismos. Un botón que alguien pulsa es
        exactamente esa acción; lo que no puede haber es que lo haga el programa.
        """
        destino = self._container.guard.upgrade_path(Capability.BROADCAST_TX)
        if destino is None:
            return
        self._container.guard.set_mode(destino)
        self._status.setText(
            f"Modo cambiado a {destino.label}. La ejecución sigue dependiendo de "
            "los topes y del interruptor de `config.toml`."
        )
        self._on_quote_selected()

    def refresh_execution_state(self) -> None:
        """Repinta lo que depende del modo, que son los botones de firmar.

        Público porque quien sabe que el modo ha cambiado es la ventana, no esta
        pestaña: `ModeGuard.subscribe` avisa a quien se apunte, y la pestaña no
        se apunta sola. Existe en vez de que la ventana llame a `_on_quote_selected`
        porque un método privado de otra clase no es una interfaz, es un atajo.

        Repinta **también la tarjeta de puentes**, y por el mismo motivo: su botón
        de firmar depende del mismo modo, y una tarjeta que se quedara con el modo
        viejo enseñaría un botón apagado —o encendido— que ya no es verdad.

        Y el saldo disponible, porque la ventana avisa por aquí de dos cosas
        distintas: que cambió el modo y que se guardó o se borró una credencial.
        Con la clave recién puesta, el saldo es justo lo que hay que ir a buscar;
        `_refresh_balance` vacía su caché sola cuando ve una dirección distinta,
        así que en un cambio de modo no vuelve a preguntar al nodo.
        """
        self._on_quote_selected()
        self._bridges.refresh_execution_state()
        self._refresh_balance()

    def _execution_blockers(
        self,
        pair: TradingPair | None = None,
        quote: Quote | None = None,
    ) -> tuple[str, ...]:
        """Todo lo que falta para poder firmar y emitir. Vacío significa que sí.

        Se devuelve la **lista de motivos** y no un booleano porque un botón
        apagado sin explicación es lo que empuja a alguien a buscar la forma de
        saltárselo. Y se devuelven **todos** y no el primero: son condiciones
        distintas —el modo se cambia aquí, `enabled` se cambia en el fichero, los
        motores y la cartera se configuran aparte— y descubrirlas de una en una,
        arreglando una para que aparezca la siguiente, es lo que hace pensar que
        la aplicación está rota en vez de a medio configurar.

        Mira las mismas cosas que mira `ExecuteSwap` antes de firmar, en el mismo
        orden: si esta lista dijera que sí y el caso de uso dijera que no, la
        interfaz estaría prometiendo algo que no cumple.

        El orden va de la ruta a la custodia: primero si la operación puede
        existir —el modo, el interruptor, un motor que sepa construir el payload—
        y después si hay con qué firmarla. Decirlo al revés llevaría a configurar
        una cartera para descubrir luego que no había por dónde.

        El par se pregunta a propósito, y no sólo la red: un motor puede construir
        en una red y no en otra —`uniswap` cubre Base pero no Solana—, así que
        «hay algún planificador» no es la misma pregunta que «hay planificador
        para el par que estoy mirando». Y con un token añadido por dirección la
        lista blanca deja de ser un detalle de configuración que se supone
        correcto: es la diferencia entre un botón que firma y uno que no, así que
        se comprueba aquí y se dice cuál falta.
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
        if pair is not None:
            permitidos = container.policy.limits.allowed_tokens
            fuera = sorted(
                symbol
                for symbol in (pair.base.symbol, pair.quote.symbol)
                if symbol not in permitidos
            )
            if fuera:
                # Se dice también **qué hay declarado**: el error más común no es
                # olvidar la lista, es escribir en ella un nombre que ningún token
                # tiene. `ETH` no es el símbolo de ningún token de la aplicación
                # —el nativo va envuelto y se llama `WETH`—, así que una lista con
                # `ETH` dentro deja fuera justo lo que se quería permitir, y sin
                # ver la lista al lado eso no se nota.
                motivos.append(
                    f"{', '.join(fuera)} no está en `allowed_tokens`, así que la "
                    f"política no deja operar con ese par (ahora declara: "
                    f"{', '.join(sorted(permitidos)) or 'nada'}; añádelo en config.toml)"
                )
        chain_key = pair.chain if pair is not None else None
        if not container.prepare_swap.is_available(chain_key):
            # Se midió: con `geckoterminal` activo —que cotiza pero no construye—
            # no hay ningún planificador, y sin este motivo el botón quedaba
            # encendido y fallaba al pulsarlo. Es justo el botón que enseña a
            # desconfiar de los botones, y el más caro de todos: el que firma.
            donde = f" en {chain_key}" if chain_key else ""
            motivos.append(
                f"no hay ningún motor activo capaz de construir el swap{donde} "
                "(activa `uniswap` o `zeroex` y dale su clave)"
            )
        if not container.keys.available():
            motivos.append("no hay cartera configurada, así que no hay con qué firmar")
        if quote is not None:
            motivos.extend(self._cap_blockers(quote))
        return tuple(motivos)

    def _cap_blockers(self, quote: Quote) -> tuple[str, ...]:
        """Lo que los topes dirían de **esta** operación, con el importe ya medido.

        Faltaba, y se notó midiendo: con el campo de cantidad en el valor que trae
        al abrir —1,0—, la compra de 1 WETH vale unos 2 605 USDC contra un tope de
        1 por operación. El botón se encendía igual, porque esta lista miraba el
        modo, el interruptor, la lista blanca, el planificador y la cartera, pero
        no el importe. El caso de uso sí lo mira, y lo mira **antes** del diálogo,
        así que no se firmaba nada; lo que pasaba es que el botón prometía una
        operación que la política iba a rechazar en cuanto se pulsara. Este es el
        botón que menos puede permitirse eso.

        El importe se saca con `reference_notional`, que es la **misma** función
        que usa `ExecuteSwap._intent`: es la única forma de que el veredicto de
        aquí y el de allí no puedan separarse. Se llama a `check_amount`, que es
        el mismo código que corre al firmar, así que el texto del motivo es el
        que el usuario vería igualmente, sólo que antes de pulsar.

        Con un par que no toca la moneda de referencia, `reference_notional`
        devuelve `None` y aquí no se dice nada: valorarlo exige red, y el gasto de
        las últimas 24 h se mueve solo, así que un «sí» tampoco sería una promesa.
        """
        notional = reference_notional(quote)
        if notional is None:
            return ()
        try:
            self._container.policy.limits.check_amount(
                notional.as_decimal(),
                spent_today=self._container.policy.spent_in_window(notional.symbol),
            )
        except ExecutionLimitExceededError as error:
            return (str(error),)
        return ()

    def _recipient_candidates(self, chain_key: str) -> list[str]:
        """Las direcciones ya configuradas que son válidas en esa red.

        Ofrecer las propias antes que un campo vacío no es comodidad: teclear una
        dirección a mano es la forma más común de perder fondos, y la lista sale
        de lo que el usuario ya declaró en su configuración.

        La **cartera de la aplicación va primera**, porque es la dirección desde
        la que sale el dinero —de ahí sale y ahí vuelve— y es la única de la lista
        que la aplicación conoce sin que nadie la haya escrito en la
        configuración. Se deriva de la clave, así que nunca es la clave.
        """
        spec = chain(chain_key)
        solana = spec.address_format is AddressFormat.SOLANA_BASE58
        check = is_solana_address if solana else is_evm_address
        candidates: list[str] = []
        propia = self._container.keys.address()
        if propia is not None and check(propia):
            candidates.append(propia)
        candidates.extend(
            address for address in self._container.settings.watch_addresses if check(address)
        )
        # Sin repetidos, conservando el orden: la cartera puede estar también en
        # `watch_addresses`, y verla dos veces en el desplegable parece un fallo.
        return list(dict.fromkeys(candidates))

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
            self._status.setText("Selecciona primero una ruta de la tabla.")
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

    # ------------------------------------------------------------------ #
    # Ejecutar: firmar y emitir
    # ------------------------------------------------------------------ #
    def _on_execute(self) -> None:
        quote = self._selected_quote()
        if quote is None:
            self._status.setText("Selecciona primero una ruta de la tabla.")
            return
        # Se vuelve a comprobar aquí aunque el botón ya estuviera apagado: el modo
        # se puede cambiar desde la barra mientras esta pestaña está abierta, y un
        # botón sólo se repinta cuando algo lo repinta. La puerta de verdad está
        # en `ExecuteSwap`; esto es para no llegar hasta ella y volver con un
        # error que se puede decir antes de molestar a nadie.
        motivos = self._execution_blockers(quote.pair, quote)
        if motivos:
            self._status.setText("No se puede ejecutar: " + " · ".join(motivos) + ".")
            return
        recipient = self._ask_recipient(quote.pair.chain)
        if recipient is None:
            return
        self._exec_btn.setEnabled(False)
        self._status.setText("Firmando y emitiendo…")
        spawn(self._do_execute(quote, recipient))

    async def _do_execute(self, quote: Quote, recipient: str) -> None:
        try:
            receipt = await self._container.execute_swap(quote, recipient=recipient)
            self._status.setText(
                f"Emitida {receipt.tx_hash} · estado {receipt.status.value}. "
                "El hash se puede contrastar en un explorador de la red."
            )
        except ConfirmationDeniedError:
            # Decir «error» aquí sería mentir: el usuario hizo lo correcto.
            self._status.setText("Cancelaste la operación. No se firmó ni se emitió nada.")
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


def _cap_height(table: QTableWidget, filas: int = 10) -> None:
    """Le pone techo al alto de una tabla: el de sus filas, no el que le sobre.

    Una tabla con dos rutas dentro de una tarjeta que estira ocupaba trescientos
    píxeles de gris vacío debajo de la última fila. Con el techo puesto, la
    tarjeta mide lo que mide su contenido y el espacio libre se lo queda la
    sección de abajo, que es otra tabla y lo aprovecha cuando tiene filas.

    Es un **máximo**, no un alto fijo: por debajo sigue encogiendo sola, que es
    lo que hace falta cuando la ventana es pequeña o cuando no hay filas. El
    tope de filas evita que veinte rutas empujen los botones fuera de la
    pantalla; a partir de ahí se desplaza, que es lo que una tabla sabe hacer.
    """
    visible = min(table.rowCount(), filas)
    # Las filas de estas tablas son de una línea (`setWordWrap(False)` al
    # construirlas): la nota es una frase larga y, envuelta, cada fila pasaba de
    # treinta píxeles a ciento cincuenta y la tabla dejaba de leerse de un
    # vistazo. Recortada se ve el principio, y el texto entero va en el tooltip.
    alto = table.horizontalHeader().height() + 2 * table.frameWidth()
    for row in range(visible):
        alto += table.rowHeight(row)
    table.setMaximumHeight(alto if visible else 0)


def _index_of(combo: QComboBox, token: Token | None) -> int:
    """Índice de un token en el desplegable, por identidad y no por etiqueta.

    Se busca por `is_same_asset` y no por texto porque las etiquetas ya no
    identifican unívocamente —`USDC` puede ser dos tokens— y porque el texto de
    una etiqueta es presentación: si mañana cambia el formato, la elección del
    usuario no tiene por qué perderse.
    """
    if token is None:
        return -1
    for index in range(combo.count()):
        candidato = combo.itemData(index)
        if isinstance(candidato, Token) and candidato.is_same_asset(token):
            return index
    return -1


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
