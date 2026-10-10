"""La tarjeta de intercambio: cuenta y red arriba, patas con saldo, y sus botones.

### Qué es

Es la tarjeta que se ve al abrir la aplicación. De arriba abajo:

1. **Dónde se opera** — la cartera que firma, el modo y la red, en la cabecera.
2. **Qué se quiere hacer** — Intercambiar, Enviar o Depositar, en un segmento.
3. **Cuánto** — las dos patas del intercambio, cada una con su token, su **saldo**
   y su importe.
4. **A qué precio** — el par, y luego la procedencia de cada cifra.
5. **El botón de cotizar**, que es el que arranca todo.

### De dónde viene, y por qué se sacó aparte

Era la «zona 1» de `pages/prices.py`. Se saca porque la página sigue siendo la
dueña del **estado** —la comparación, el payload preparado, la ejecución y el
botón que firma— mientras esta tarjeta es la vista que lo pide. Tener las dos
cosas en un fichero de mil seiscientas líneas hacía que cambiar el ancho de un
campo tocara el mismo sitio que construir una transacción.

### Los dos desplegables ocultos, y por qué no son un resto

`_base` y `_contra` siguen siendo la **fuente de verdad** de qué token está en cada
pata. La cara visible es un botón que abre el selector en modal —porque el modal
puede enseñar saldos y un desplegable no—, pero el estado sigue en los combos: es
lo que permite que `legs()`, la búsqueda por identidad y todo lo que ya estaba
probado sigan funcionando exactamente igual. La interfaz cambia de traje; el
estado no se mueve de sitio.

### Lo que NO decide

Nada de política. Si se puede firmar lo dice `ui/execution_gate.py`, que llama a
las mismas comprobaciones que correrán al firmar. Esta tarjeta no firma, no emite y
no toca ninguna clave.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.execution import ANY_TOKEN
from amigocompora.domain.models import Token
from amigocompora.domain.money import BasisPoints
from amigocompora.ui import icons
from amigocompora.ui.theme import (
    COLOR_ACCENT,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_SUCCESS,
)
from amigocompora.ui.wallet_state import WalletBalances
from amigocompora.ui.widgets import (
    Card,
    Chip,
    Field,
    format_amount,
    token_labels,
    tokens_for_chain,
)

#: Los tres modos de la tarjeta, en el orden en que se ven.
TAB_SWAP: Final = 0
TAB_SEND: Final = 1
TAB_RECEIVE: Final = 2


def _shorten(address: str) -> str:
    """Una dirección, acortada para un chip. La entera va en el tooltip."""
    if len(address) <= 14:
        return address
    return f"{address[:6]}…{address[-4:]}"


def position_of(combo: QComboBox, token: Token | None) -> int:
    """Índice de un token en un desplegable, por **identidad**, no por etiqueta.

    Se busca con `is_same_asset` porque las etiquetas no identifican un token
    —en Polygon el USDC nativo y el puenteado publican el mismo símbolo— y porque
    el texto de una etiqueta es presentación: si mañana cambia el formato, la
    elección del usuario no tiene por qué perderse. Devuelve -1 si no está, para
    que quien llame distinga «no está» de «está el primero».
    """
    if token is None:
        return -1
    for index in range(combo.count()):
        candidato = combo.itemData(index)
        if isinstance(candidato, Token) and candidato.is_same_asset(token):
            return index
    return -1


class AmountSpinBox(QDoubleSpinBox):
    """Un importe que se escribe y se lee **sin ceros de relleno**: «5», no «5,000000».

    El campo guarda seis decimales porque es lo que admite la cantidad, pero
    enseñarlos siempre llena la pantalla de ceros y hace difícil leer de un vistazo
    qué se va a entregar. Aquí el texto se recorta sólo en la pantalla: el valor
    que se firma sale de `value()`, con todos sus decimales, y no de este texto.
    Se usa el separador decimal del idioma del sistema, para que lo que se escribe
    y lo que se lee sean el mismo formato.
    """

    def textFromValue(self, value: float) -> str:
        texto = self.locale().toString(float(value), "f", self.decimals())
        separador = self.locale().decimalPoint()
        if separador in texto:
            texto = texto.rstrip("0").rstrip(separador)
        return texto or "0"


class TokenLeg(QFrame):
    """Una pata del par: su título, su importe, su token y su saldo.

    El botón del token —y no un desplegable— es la diferencia de fondo con lo que
    había: abre el selector en modal, donde hay buscador, saldos y valoración. Un
    `QComboBox` no puede enseñar cuánto tienes, que es el dato que decide cuál se
    elige.
    """

    token_clicked = Signal()
    max_clicked = Signal()
    #: Volver a cotizar con el importe que hay ahora mismo, sin esperar la pausa.
    refresh_clicked = Signal()

    def __init__(
        self,
        caption: str,
        *,
        editable: bool,
        with_unit_label: bool = False,
        with_refresh: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("legBox")
        self._caption_text = caption
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(6)

        cabecera = QHBoxLayout()
        cabecera.setContentsMargins(0, 0, 0, 0)
        self._caption = QLabel(caption)
        self._caption.setObjectName("sectionTitle")
        cabecera.addWidget(self._caption)
        cabecera.addStretch()
        self._max_btn = QPushButton("Máx")
        self._max_btn.setObjectName("link")
        self._max_btn.setToolTip(
            "Pone el importe entero del saldo disponible. El campo redondea a seis "
            "decimales, así que para un token de dieciocho cifras deja fuera lo que "
            "no cabe en seis —siempre por debajo del saldo, nunca por encima."
        )
        self._max_btn.clicked.connect(self.max_clicked.emit)
        # «Máx» sólo tiene sentido en una pata que se escribe. En la de «recibes» no
        # hay importe que poner: se queda oculto, a diferencia de la pata de entrega,
        # donde se mantiene visible aunque apagado cuando no hay saldo.
        self._max_btn.setVisible(editable)
        # Siempre visible: un «Máx» que aparece y desaparece según el saldo hace que
        # el usuario no sepa si la función existe. Cuando no hay saldo se apaga y su
        # tooltip dice por qué (ver `_paint_leg`).
        if with_refresh:
            # El icono de actualizar junto al importe: vuelve a cotizar ya, sin esperar
            # la pausa. Está aquí y no en otro sitio porque es el importe el que cambia.
            self._refresh_btn = QPushButton("⟳")
            self._refresh_btn.setObjectName("link")
            self._refresh_btn.setToolTip("Volver a cotizar con este importe")
            self._refresh_btn.clicked.connect(self.refresh_clicked.emit)
            cabecera.addWidget(self._refresh_btn)
        cabecera.addWidget(self._max_btn)
        lay.addLayout(cabecera)

        fila = QHBoxLayout()
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(8)

        self._amount = AmountSpinBox()
        self._amount.setRange(0.000001, 1_000_000)
        self._amount.setDecimals(6)
        self._amount.setValue(1.0)
        self._amount.setObjectName("amountInput")
        self._amount.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self._amount.setFrame(False)
        self._amount.setMinimumWidth(150)
        self._amount.setReadOnly(not editable)
        self._amount.setVisible(editable)

        #: La cifra de la pata que se recibe: no se escribe, se lee.
        self._readout = QLabel("—")
        self._readout.setObjectName("bigNumber")
        self._readout.setStyleSheet(f"color: {COLOR_MUTED};")
        self._readout.setMinimumWidth(150)
        self._readout.setVisible(not editable)

        #: La unidad del importe que se escribe, pegada a la cifra. Sin ella el
        #: mismo «1» es un token o mil dólares, y esto acaba en una firma.
        self._unit = QLabel("")
        self._unit.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 700;")
        self._unit.setVisible(with_unit_label)

        self._token_btn = QPushButton("Elegir token")
        self._token_btn.setObjectName("tokenButton")
        self._token_btn.clicked.connect(self.token_clicked.emit)

        fila.addWidget(self._amount, 1)
        fila.addWidget(self._readout, 1)
        fila.addWidget(self._unit)
        fila.addWidget(self._token_btn, 0, Qt.AlignVCenter)
        lay.addLayout(fila)

        self._balance = QLabel("")
        self._balance.setObjectName("hint")
        self._balance.setWordWrap(True)
        lay.addWidget(self._balance)

    # Acceso explícito a las piezas, en vez de atributos sueltos: quien pinta
    # desde fuera no debería poder cambiarlas de sitio.
    @property
    def amount(self) -> QDoubleSpinBox:
        return self._amount

    @property
    def readout(self) -> QLabel:
        return self._readout

    @property
    def balance(self) -> QLabel:
        return self._balance

    @property
    def max_btn(self) -> QPushButton:
        return self._max_btn

    @property
    def unit(self) -> QLabel:
        return self._unit

    def set_caption(self, text: str) -> None:
        self._caption_text = text
        self._caption.setText(text)

    def set_token_label(self, text: str, *, icon_key: str | None = None) -> None:
        """El nombre del token en el botón, con su icono delante si se conoce.

        `icon_key` puede no coincidir con el texto: el USDC puenteado de Polygon
        publica el símbolo «USDC» y así se enseña, pero el nombre con el que está
        en el catálogo de iconos es «USDC.e». Sin clave se quita el icono: «Elegir
        token» no es una marca y no puede llevar logo.
        """
        self._token_btn.setText(text)
        self._token_btn.setIcon(icons.token_icon(icon_key) if icon_key else QIcon())
        self._token_btn.setToolTip(icon_key or "")

    def set_unit(self, text: str) -> None:
        self._unit.setText(text)

    def set_readout(self, text: str, *, bright: bool = True) -> None:
        self._readout.setText(text)
        self._readout.setStyleSheet("" if bright else f"color: {COLOR_MUTED};")


class SwapCard(QWidget):
    """La tarjeta de intercambio: cabecera, segmento y los tres formularios."""

    #: Hay que cotizar el par que describen los controles.
    quote_requested = Signal()
    #: Hay que invertir las dos patas.
    invert_requested = Signal()
    #: Se pulsó el botón de token de una pata: `"base"`, `"quote"` o `"send"`.
    token_picked = Signal(str)
    #: Hay que releer los saldos de las redes que se están mirando.
    balances_requested = Signal()
    #: Hay que firmar y emitir la retirada que describe el formulario de envío.
    send_requested = Signal()
    #: Se pulsó el engranaje: hay que configurar el deslizamiento por defecto.
    slippage_requested = Signal()

    def __init__(
        self,
        container: Container,
        balances: WalletBalances,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._balances = balances

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build())

    # ------------------------------------------------------------------ #
    # Construcción
    # ------------------------------------------------------------------ #
    def _build(self) -> QWidget:
        # Sin subtítulo: en el ancho de la ventana la cabecera tiene que caber la
        # cuenta, el modo y la red, y el subtítulo era el que empujaba la tarjeta
        # más allá del borde derecho. Lo que explicaba ya lo dice la propia pantalla.
        card = Card("Intercambiar")
        self._card = card

        # La cuenta, el modo y la red, en la cabecera. Es el contexto de todo lo
        # que hay debajo: el importe que se escribe dos centímetros más abajo
        # depende de las tres, y responder «¿en qué red estoy?» desde un panel de
        # ajustes obligaría a irse a otro sitio justo antes de escribir la cifra.
        self._account = Chip("sin cartera", COLOR_MUTED)
        self._account.setToolTip(
            "La cartera con la que se firma. Se configura en la pestaña de Motores, "
            "en Credenciales."
        )
        card.header.addWidget(self._account)

        self._mode_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._mode_chip)

        self._chain = QComboBox()
        for key in sorted(CHAINS):
            self._chain.addItem(icons.network_icon(key), f"{CHAINS[key].name} ({key})", key)
        # El ancho natural de un `QComboBox` es el de su ítem **más largo**, y aquí
        # eso son cuatrocientos píxeles por «Robinhood Chain (robinhood)»: la mitad
        # de la cabecera gastada en un nombre que se lee entero al desplegarlo.
        # Con `AdjustToMinimumContentsLengthWithIcon` el ancho pasa a depender de
        # un número de caracteres elegido, que es lo que se puede defender en una
        # pantalla donde el sitio lo comparten la cuenta, el modo y la red.
        self._chain.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._chain.setMinimumContentsLength(16)
        self._chain.setToolTip("La red en la que se opera. Cambiarla rearma las dos patas.")
        # La red va en su propia fila, debajo de la cabecera y con su etiqueta. En la
        # cabecera competía con dos chips y el botón de saldos: entre los cuatro
        # pedían 952 px, y la tarjeta entera se salía de la ventana.
        red = Field("RED")
        red.add(self._chain, 1)
        card.body().addWidget(red)

        self._refresh_btn = QPushButton("Saldos")
        self._refresh_btn.setObjectName("secondary")
        self._refresh_btn.setToolTip(
            "Vuelve a leer los saldos de las redes que se están mirando. Se leen "
            "solos al cambiar de red o de token; esto es para forzarlo."
        )
        self._refresh_btn.clicked.connect(self.balances_requested.emit)
        card.header.addWidget(self._refresh_btn)

        # El engranaje del deslizamiento, en la esquina superior derecha. Está en
        # la cabecera —y no escondido en Ajustes— porque es un número que cambia
        # lo que se firma: el mínimo que un swap acepta recibir. El tooltip dice
        # el valor vigente, para no tener que abrir el diálogo sólo para mirarlo.
        self._slippage_btn = QPushButton("⚙")
        self._slippage_btn.setObjectName("link")
        self._slippage_btn.setMinimumWidth(28)
        self._slippage_btn.clicked.connect(self.slippage_requested.emit)
        card.header.addWidget(self._slippage_btn)
        self.set_slippage_bps(self._container.slippage.bps)

        card.add_row(self._build_segment())
        self._forms = QStackedWidget()
        self._forms.addWidget(self._build_swap_form())
        self._forms.addWidget(self._build_send_form())
        self._forms.addWidget(self._build_receive_form())
        card.body().addWidget(self._forms)
        return card

    def _build_segment(self) -> QHBoxLayout:
        """Las tres operaciones de una cartera, en una fila.

        Están aquí y no repartidas por la ventana porque son las tres cosas que se
        hacen con una cartera y ninguna es más importante que otra. Con «depositar»
        en un diálogo y «enviar» en otra pantalla, lo que hay que recordar no es la
        operación: es dónde está.
        """
        fila = QHBoxLayout()
        fila.setSpacing(6)
        self._segment_btns: list[QPushButton] = []
        for indice, texto in enumerate(("Intercambiar", "Enviar", "Depositar")):
            btn = QPushButton(texto)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setChecked(indice == TAB_SWAP)
            btn.clicked.connect(lambda _=False, i=indice: self._show_form(i))
            fila.addWidget(btn)
            self._segment_btns.append(btn)
        fila.addStretch()
        return fila

    # ----------------------------------------------------- intercambiar #
    def _build_swap_form(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._give = TokenLeg("ENTREGAS", editable=True, with_unit_label=True, with_refresh=True)
        self._give.refresh_clicked.connect(self.quote_requested.emit)
        self._give.token_clicked.connect(lambda: self.token_picked.emit("base"))
        self._give.max_clicked.connect(self._on_max)
        lay.addWidget(self._give)

        # El botón de invertir, centrado entre las dos patas, que es donde se lee
        # lo que hace. Lleva una **palabra** y no el glifo suelto: dibujado con la
        # fuente del sistema, «⇅» sale como un signo diminuto que se lee como una
        # letra suelta, y este es el único control de la tarjeta que no se entiende
        # por lo que tiene al lado.
        medio = QHBoxLayout()
        medio.addStretch()
        self._invert_btn = QPushButton("Invertir")
        self._invert_btn.setObjectName("invert")
        self._invert_btn.setToolTip(
            "Invertir el par: pasa lo que entregas a lo que recibes y cotiza la "
            "operación contraria."
        )
        self._invert_btn.clicked.connect(self.invert_requested.emit)
        medio.addWidget(self._invert_btn)
        medio.addStretch()
        lay.addLayout(medio)

        self._want = TokenLeg("RECIBES (estimado)", editable=False)
        self._want.token_clicked.connect(lambda: self.token_picked.emit("quote"))
        lay.addWidget(self._want)

        # Los dos desplegables de siempre, ocultos: son la fuente de verdad de qué
        # token está en cada pata. Ver el docstring del módulo.
        self._base = QComboBox()
        self._contra = QComboBox()
        for combo in (self._base, self._contra):
            combo.setVisible(False)
        self._base.currentIndexChanged.connect(self._on_legs_changed)
        self._contra.currentIndexChanged.connect(self._on_legs_changed)
        lay.addWidget(self._base)
        lay.addWidget(self._contra)

        contexto = QHBoxLayout()
        self._pair = QLabel("—")
        self._pair.setStyleSheet(f"color: {COLOR_MUTED}; font-weight: 600;")
        contexto.addWidget(self._pair)
        contexto.addStretch()
        lay.addLayout(contexto)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._quote_btn = QPushButton("Cotizar rutas")
        self._quote_btn.setMinimumHeight(34)
        self._quote_btn.setToolTip(
            "Pregunta a todos los motores activos y compara las rutas. Cotizar no "
            "firma ni emite nada: sólo pregunta precios."
        )
        self._quote_btn.clicked.connect(self.quote_requested.emit)
        # Ya no hace falta pulsarlo: cotizar es automático al cambiar el importe, la
        # red o el par, y el ⟳ junto al importe fuerza una cotización. Se deja el
        # botón oculto y no borrado, porque la página todavía lo conecta y lo usa.
        self._quote_btn.setVisible(False)
        lay.addWidget(self._quote_btn)
        return page

    # ----------------------------------------------------------- enviar #
    def _build_send_form(self) -> QWidget:
        """Enviar: token, importe y destino, todo en la misma tarjeta.

        Sustituye al `QInputDialog` que pedía el destinatario **después** de elegir
        la ruta: un modal que interrumpe para preguntar algo que se puede dejar
        escrito. Y con la propia cartera ofrecida por delante, que es lo que evita
        el error más caro de una transferencia —teclearla a mano—.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        token_field = Field("QUÉ ENVÍAS")
        self._send_token = QComboBox()
        # Igual que el de la red: el ancho lo fija un número de caracteres y no el
        # ítem más largo, que aquí es un `symbol` con la dirección pegada para
        # desempatar homónimos.
        self._send_token.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._send_token.setMinimumContentsLength(12)
        self._send_token.currentIndexChanged.connect(self._on_send_token_changed)
        token_field.add(self._send_token, 1)
        lay.addWidget(token_field)

        self._send_leg = TokenLeg("ENVÍAS", editable=True, with_unit_label=True)
        self._send_leg.token_clicked.connect(lambda: self.token_picked.emit("send"))
        self._send_leg.max_clicked.connect(self._on_max)
        lay.addWidget(self._send_leg)

        destino = Field("DESTINO")
        self._recipient = QComboBox()
        self._recipient.setEditable(True)
        # Sin mínimo fijo: un `QStackedWidget` mide por la página más ancha aunque
        # esté oculta, así que este campo de 340 px estaba empujando la tarjeta de
        # intercambio más allá del borde de la ventana. Se estira con el panel.
        self._recipient.setMinimumWidth(180)
        self._recipient.setToolTip(
            "La dirección que recibe. Se ofrecen las tuyas por delante porque "
            "teclear una a mano es la forma más común de perder fondos, y la "
            "lista sale de lo que ya declaraste en tu configuración."
        )
        destino.add(self._recipient, 1)
        lay.addWidget(destino)

        self._send_status = QLabel("")
        self._send_status.setObjectName("hint")
        self._send_status.setWordWrap(True)
        lay.addWidget(self._send_status)

        self._send_btn = QPushButton("Enviar: firmar y emitir…")
        self._send_btn.setObjectName("danger")
        self._send_btn.setMinimumHeight(36)
        self._send_btn.setToolTip(
            "Firma y emite una transferencia. Es irreversible: lo que salga no "
            "vuelve, y a una dirección equivocada no hay a quién reclamarle."
        )
        self._send_btn.clicked.connect(self.send_requested.emit)
        lay.addWidget(self._send_btn)
        return page

    # -------------------------------------------------------- depositar #
    def _build_receive_form(self) -> QWidget:
        """Recibir: la dirección y su QR, en línea.

        Era un diálogo, y un diálogo para enseñar una dirección pública es una
        parada de más: no hay nada que aceptar, sólo algo que copiar.
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._deposit_warning = QLabel("")
        self._deposit_warning.setTextFormat(Qt.RichText)
        self._deposit_warning.setWordWrap(True)
        lay.addWidget(self._deposit_warning)

        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.setMinimumHeight(180)
        lay.addWidget(self._qr, 0, Qt.AlignCenter)

        fila = QHBoxLayout()
        self._deposit_address = QLabel("")
        self._deposit_address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._deposit_address.setWordWrap(True)
        self._deposit_address.setStyleSheet("font-family: 'Consolas', monospace;")
        fila.addWidget(self._deposit_address, 1)
        self._copy_btn = QPushButton("Copiar dirección")
        self._copy_btn.setObjectName("secondary")
        self._copy_btn.clicked.connect(self._copy_address)
        fila.addWidget(self._copy_btn)
        lay.addLayout(fila)

        self._copy_notice = QLabel("")
        self._copy_notice.setObjectName("hint")
        self._copy_notice.setWordWrap(True)
        lay.addWidget(self._copy_notice)
        return page

    # ------------------------------------------------------------------ #
    # Estado que viene de fuera
    # ------------------------------------------------------------------ #
    def current_chain(self) -> str:
        return str(self._chain.currentData() or "")

    def chain_combo(self) -> QComboBox:
        """El desplegable de red, para que la página lo conecte."""
        return self._chain

    def legs(self) -> tuple[Token | None, Token | None]:
        """Las dos patas, en el orden en que se mueven. `None` si no hay token.

        `entrega` es lo que sale de la cartera y `recibe` lo que entra. El par se
        arma con ellas y **no se vuelve a consultar la dirección en ningún sitio**
        de los de después: con esta traducción repartida por la pantalla, el
        importe acabaría firmado al revés en alguna vista.
        """
        entrega = self._base.currentData()
        recibe = self._contra.currentData()
        if not isinstance(entrega, Token) or not isinstance(recibe, Token):
            return None, None
        return entrega, recibe

    def set_legs(self, entrega: Token | None, recibe: Token | None) -> None:
        """Pone las dos patas por **identidad**, que es lo único que identifica."""
        if entrega is not None:
            indice = position_of(self._base, entrega)
            if indice >= 0:
                self._base.setCurrentIndex(indice)
        if recibe is not None:
            indice = position_of(self._contra, recibe)
            if indice >= 0:
                self._contra.setCurrentIndex(indice)
        self.refresh_leg_labels()

    def reload_tokens(self, entrega: Token | None, recibe: Token | None) -> None:
        """Rearma las dos listas de la red actual conservando lo elegido.

        Se conservan las patas si siguen existiendo en la red nueva, y si no se cae
        a las dos monedas con las que de verdad hay mercado: el envoltorio nativo
        —que es con lo que operan los AMM— para lo que se entrega, y la stablecoin
        de referencia para lo que se recibe. Conservarlas importa porque esto
        también se llama al añadir un token por dirección: repintar las listas no
        es motivo para deshacer una elección que el usuario acaba de hacer.
        """
        tokens = tokens_for_chain(self._container.token_store, self.current_chain())
        etiquetas = token_labels(tokens)
        for combo in (self._base, self._contra):
            combo.blockSignals(True)
            combo.clear()
            for etiqueta, token in zip(etiquetas, tokens, strict=True):
                combo.addItem(icons.token_icon(token.display_symbol), etiqueta, token)
            combo.blockSignals(False)

        from amigocompora.engines.catalog import quote_token, wrapped_native

        red = self.current_chain()
        if entrega is not None and position_of(self._base, entrega) < 0:
            entrega = wrapped_native(red)
        if recibe is not None and position_of(self._contra, recibe) < 0:
            recibe = quote_token(red) or wrapped_native(red)
        self.set_legs(entrega, recibe)
        self._mark_unusable_tokens()

    def _mark_unusable_tokens(self) -> None:
        """Marca los tokens con los que la política no dejaría firmar.

        Se marca aquí y no al pulsar «Ejecutar» porque descubrirlo al final es
        descubrirlo tarde: el usuario habría cotizado, elegido ruta y llegado hasta
        el diálogo para que le dijeran que ese token no está en la lista blanca.

        Se pinta el **texto** en gris y se explica en el tooltip, sin tocar la
        etiqueta: la etiqueta identifica el token y hay código que la busca por
        texto, así que añadirle un sufijo rompería una búsqueda para arreglar un
        aviso que cabe en el color. Con la lista blanca vacía no se marca nada:
        vacía significa «no hay nada permitido», y pintar todo de gris describiría
        una aplicación rota en vez de una a medio configurar.
        """
        limites = self._container.policy.limits
        permitidos = limites.allowed_tokens
        # Vacía o con el comodín «*», no hay nada que marcar: todo se puede operar.
        if not permitidos or ANY_TOKEN in permitidos:
            return
        for combo in (self._base, self._contra, self._send_token):
            for index in range(combo.count()):
                token = combo.itemData(index)
                # Quién pasa lo decide `allows_token`, no una comparación en
                # crudo: el catálogo tiene símbolos en caja mixta (`pUSD`) y
                # marcarlos en gris cuando la lista sí los permite describiría
                # una aplicación rota.
                if not isinstance(token, Token) or limites.allows_token(token.symbol):
                    continue
                combo.setItemData(index, QColor(COLOR_MUTED), Qt.ItemDataRole.ForegroundRole)
                combo.setItemData(
                    index,
                    f"«{token.symbol}» no está en `allowed_tokens`, así que la "
                    "política no dejará firmar una operación que lo toque. Se puede "
                    "cotizar igual: cotizar no mueve dinero.",
                    Qt.ItemDataRole.ToolTipRole,
                )

    def refresh_leg_labels(self) -> None:
        """Qué se entrega y qué se recibe, y en qué unidad va la cantidad.

        La cantidad se escribe en la unidad de la pata que **se entrega**, y esa
        pata la elige un botón que está al lado. Por eso la etiqueta no es
        decoración: sin ella el mismo número significa dos cosas distintas, y esto
        acaba en una firma.
        """
        entrega, recibe = self.legs()
        if entrega is None or recibe is None:
            self._pair.setText("—")
            return
        self._pair.setText(f"{entrega.symbol} → {recibe.symbol}")
        self._give.set_token_label(entrega.symbol, icon_key=entrega.display_symbol)
        self._give.set_unit(entrega.symbol)
        self._want.set_token_label(recibe.symbol, icon_key=recibe.display_symbol)
        self._give.set_caption(f"ENTREGAS · {entrega.symbol}")
        self._want.set_caption(f"RECIBES · {recibe.symbol}")
        # La estimación es de la ruta anterior: en cuanto cambia el par deja de
        # valer, y una cifra vieja junto a un par nuevo es una cifra falsa.
        self._want.set_readout("—", bright=False)

    def _on_legs_changed(self) -> None:
        self.refresh_leg_labels()
        self.refresh_balances()

    # ------------------------------------------------------------------ #
    # Saldos
    # ------------------------------------------------------------------ #
    def refresh_balances(self) -> None:
        """Pide el saldo de las redes que se están mirando y pinta lo que haya.

        Se pinta **siempre** desde lo que ya está en la caché y sólo se pide lo que
        falta: así un repintado por cambiar de token no gasta una petición de red,
        que es justo lo que el estado compartido existe para evitar.
        """
        owner = self._container.keys.address() or ""
        for cadena in self._painted_chains():
            self._balances.ensure(cadena, owner=owner)
        self._paint_balances()

    def _painted_chains(self) -> tuple[str, ...]:
        """Las redes que esta tarjeta está enseñando ahora mismo."""
        cadenas = [self.current_chain()]
        entrega, recibe = self.legs()
        for token in (entrega, recibe, self.selected_send_token()):
            if token is not None:
                cadenas.append(token.chain)
        # Sin repetidos y sin vacíos, conservando el orden.
        return tuple(dict.fromkeys(c for c in cadenas if c))

    def paint(self) -> None:
        """Repinta los saldos. La llama la página cuando llega una lectura."""
        self._paint_balances()

    def _paint_balances(self) -> None:
        entrega, recibe = self.legs()
        self._paint_leg(self._give, entrega)
        self._paint_leg(self._want, recibe)
        self._paint_leg(self._send_leg, self.selected_send_token())

    def _paint_leg(self, leg: TokenLeg, token: Token | None) -> None:
        """El saldo de una pata, con sus tres desenlaces bien distintos.

        «0» y «no se pudo leer» son cosas distintas y no pueden escribir lo mismo:
        un fallo de red pintado como un cero lleva a no operar sin motivo, y un cero
        pintado como un fallo lleva a buscar un problema que no existe.
        """
        if token is None:
            leg.balance.setText("")
            leg.max_btn.setEnabled(False)
            leg.max_btn.setEnabled(False)
            return
        if not self._container.keys.address():
            sin_cartera = (
                "No hay ninguna cartera configurada: pon la clave privada en la "
                "pestaña de Motores, en Credenciales, y aparecerá aquí el saldo "
                "disponible."
            )
            leg.balance.setText("")
            leg.balance.setStyleSheet(f"color: {COLOR_MUTED};")
            leg.balance.setToolTip(sin_cartera)
            # El «Máx» se **apaga** además de esconderse, y no es lo mismo: el
            # estado del botón es lo que consulta quien pregunta si se puede
            # operar, y un botón escondido pero encendido seguiría contestando que
            # sí. El tooltip lleva el mismo motivo que el saldo, porque sin cartera
            # el límite que impide pulsarlo no son los seis decimales.
            leg.max_btn.setEnabled(False)
            leg.max_btn.setEnabled(False)
            leg.max_btn.setToolTip(sin_cartera)
            return

        cadena = self._balances.holdings(token.chain)
        if cadena is None:
            en_vuelo = self._balances.reading(token.chain)
            leg.balance.setText("Leyendo el saldo…" if en_vuelo else "")
            leg.balance.setStyleSheet(f"color: {COLOR_MUTED};")
            leg.max_btn.setEnabled(False)
            leg.max_btn.setEnabled(False)
            return
        if cadena.failed:
            leg.balance.setText(f"No se pudo leer el saldo: {cadena.error}")
            leg.balance.setStyleSheet(f"color: {COLOR_DANGER};")
            leg.max_btn.setEnabled(False)
            leg.max_btn.setEnabled(False)
            return

        holding = self._balances.find(token.chain, token)
        if holding is None:
            leg.balance.setText(
                f"Sin datos de {token.symbol} en la lectura de {CHAINS[token.chain].name}."
            )
            leg.balance.setStyleSheet(f"color: {COLOR_MUTED};")
            leg.max_btn.setEnabled(False)
            leg.max_btn.setEnabled(False)
            return

        cantidad = format_amount(holding.as_decimal())
        leg.balance.setText(f"Disponible: {cantidad} {token.symbol}")
        leg.balance.setStyleSheet("")
        # El tooltip lleva la cifra **entera**: el recorte es de la pantalla y tener
        # que irse a la cartera para comprobar un saldo sería esconder un dato ya
        # calculado.
        pistas = [f"{holding.as_decimal():f} {token.symbol}"]
        if cadena.missing:
            pistas.append("Falta: " + "; ".join(cadena.missing))
        if token.is_native:
            pistas.append(
                "Es la moneda de la red, así que la comisión de la operación sale "
                "de este mismo saldo: entregarlo entero no cabe."
            )
        leg.balance.setToolTip("\n".join(pistas))
        # Se apaga **y** se esconde cuando no hay saldo, en vez de sólo esconderse:
        # el estado del botón es lo que consulta quien pregunta si se puede operar,
        # y uno escondido pero encendido seguiría contestando que sí.
        hay_saldo = holding.amount.raw > 0
        leg.max_btn.setEnabled(hay_saldo)
        if not hay_saldo:
            leg.max_btn.setToolTip(f"No hay saldo de {token.symbol} en esta red.")

    def apply_max(self) -> None:
        """Pone el importe entero, y dice lo que el campo no puede escribir.

        `QDoubleSpinBox` tiene seis decimales y el saldo puede tener dieciocho, así
        que el campo escribe **menos** de lo que hay —nunca más, que es la dirección
        segura del redondeo—. Se dice cuánto queda fuera en vez de callarlo: un
        «Máx» que deja 0,000000000001 ETH sin poner y no lo dice es un botón que
        miente sobre lo que hace.

        Opera sobre la pata que se está mirando. Se pregunta por la **visible** y no
        por la que se pulsó porque el botón vive dentro de su pata y hay una por
        formulario: la del intercambio y la del envío son dos botones distintos, y
        el efecto tiene que caer en la pata que los contiene.
        """
        leg = self._send_leg if self._send_leg.isVisible() else self._give
        token = self._token_of(leg)
        if token is None:
            return
        holding = self._balances.find(token.chain, token)
        if holding is None or holding.is_empty:
            return
        completo = holding.as_decimal()
        # El campo redondea al más cercano, no hacia abajo: 0,0928237 pasaba a
        # 0,092824, por encima del saldo, y el swap revertía con TRANSFER_FROM_FAILED.
        por_debajo = completo.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        leg.amount.setValue(float(por_debajo))
        escrito = Decimal(str(leg.amount.value()))
        if escrito < completo:
            self.set_status(
                f"Máx: {format_amount(escrito, full=True)} de "
                f"{format_amount(completo, full=True)} {token.symbol} — el campo "
                f"tiene seis decimales y no cabe el resto."
            )
        else:
            self.set_status("")

    def _on_max(self) -> None:
        """El botón «Máx» de una pata. El trabajo lo hace `apply_max`."""
        self.apply_max()

    def _token_of(self, leg: TokenLeg) -> Token | None:
        if leg is self._give:
            return self.legs()[0]
        if leg is self._want:
            return self.legs()[1]
        return self.selected_send_token()

    # ------------------------------------------------------------------ #
    # Enviar
    # ------------------------------------------------------------------ #
    def reload_send_tokens(self) -> None:
        """Rearma el desplegable de la pata de envío conservando lo elegido."""
        previo = self.selected_send_token()
        tokens = tokens_for_chain(self._container.token_store, self.current_chain())
        etiquetas = token_labels(tokens)
        self._send_token.blockSignals(True)
        self._send_token.clear()
        for etiqueta, token in zip(etiquetas, tokens, strict=True):
            self._send_token.addItem(icons.token_icon(token.display_symbol), etiqueta, token)
        indice = position_of(self._send_token, previo)
        self._send_token.setCurrentIndex(max(indice, 0))
        self._send_token.blockSignals(False)
        self._on_send_token_changed()

    def _on_send_token_changed(self) -> None:
        token = self.selected_send_token()
        self._send_leg.set_token_label(
            token.symbol if token else "Elegir token",
            icon_key=token.display_symbol if token else None,
        )
        self._send_leg.set_unit(token.symbol if token else "")
        self._send_leg.set_caption(
            f"ENVÍAS · {token.symbol}" if token else "ENVÍAS"
        )
        self.refresh_balances()

    def selected_send_token(self) -> Token | None:
        dato = self._send_token.currentData()
        return dato if isinstance(dato, Token) else None

    def amount_value(self) -> Decimal:
        """El importe que se entrega, en unidades humanas, para el intercambio.

        Se pasa a `Decimal` a partir del `value()` del campo y no de su texto: el
        texto de pantalla no tiene por qué ser el número exacto, y el número que se
        firma tiene que ser el que se escribió. Igual que `send_amount`, la
        conversión a la escala del token la hace quien lo usa, con sus decimales.
        """
        return Decimal(str(self._give.amount.value()))

    def send_amount(self) -> Decimal:
        """El importe a enviar, en unidades humanas.

        Se lee del campo como **texto** y se convierte una sola vez, en el caso de
        uso, con los decimales del token. Convertirlo aquí a la escala de este
        widget redondearía 0,0000012 a 0,000001 sin decirlo, y eso es un `raw` de
        menos en una transferencia que no se puede deshacer.
        """
        return Decimal(str(self._send_leg.amount.value()))

    def set_send_status(self, text: str) -> None:
        self._send_status.setText(text)

    def set_recipients(self, addresses: list[str]) -> None:
        """Ofrece las direcciones conocidas, la propia por delante."""
        actual = self._recipient.currentText().strip()
        self._recipient.clear()
        self._recipient.addItems(addresses)
        self._recipient.setEditText(actual or (addresses[0] if addresses else ""))

    def recipient(self) -> str:
        return self._recipient.currentText().strip()

    def set_send_enabled(self, enabled: bool, reason: str = "") -> None:
        self._send_btn.setEnabled(enabled)
        self._send_btn.setToolTip(
            reason or self._send_btn.toolTip()
        )

    # ------------------------------------------------------------------ #
    # Depositar
    # ------------------------------------------------------------------ #
    def refresh_deposit(self) -> None:
        """La dirección de la red actual y su QR.

        El QR lleva la dirección **sola**, sin prefijo `ethereum:` ni importe: un
        prefijo hace que muchas carteras pidan confirmar la red, y aquí la red es
        una elección de la cabecera. Ponerla dentro del código haría que el QR
        dijera una red distinta de la que está seleccionada arriba.
        """
        red = self.current_chain()
        direccion = self._container.keys.address() or ""
        if direccion:
            self._deposit_warning.setText(
                f"Envía sólo activos de <b>{CHAINS[red].name}</b> a esta dirección. "
                f"Lo que llegue por otra red <b>no se recupera</b>: no hay a quién "
                f"reclamárselo."
            )
            self._deposit_warning.setStyleSheet(f"color: {COLOR_MUTED};")
        else:
            self._deposit_warning.setText(
                "No hay ninguna cartera configurada, así que no hay dirección que "
                "enseñar. Pon la clave privada en la pestaña de Motores, en "
                "Credenciales."
            )
            self._deposit_warning.setStyleSheet(f"color: {COLOR_MUTED};")
        self._deposit_address.setText(direccion or "—")
        self._qr.setPixmap(qr_pixmap(direccion) if direccion else QPixmap())
        self._copy_btn.setEnabled(bool(direccion))
        self._copy_notice.setText("")

    def _copy_address(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._deposit_address.text())
        self._copy_notice.setText(
            "Copiada. Se queda en el portapapeles hasta que algo la sustituya."
        )

    # ------------------------------------------------------------------ #
    # Pintado desde la página
    # ------------------------------------------------------------------ #
    def set_owner(self, address: str | None) -> None:
        """Pinta la cartera que firma. **Nunca la clave**, sólo la dirección."""
        if not address:
            self._account.setText("sin cartera")
            self._account.set_color(COLOR_MUTED)
            self._account.setToolTip(
                "No hay ninguna clave configurada, así que no hay cartera con la que "
                "firmar. Se configura en la pestaña de Motores, en Credenciales."
            )
            return
        self._account.setText(_shorten(address))
        self._account.set_color(COLOR_ACCENT)
        self._account.setToolTip(
            f"{address}\n\nEs la cartera con la que se firma y desde la que saldría "
            f"el dinero."
        )

    def set_mode(self, label: str, *, can_sign: bool) -> None:
        self._mode_chip.set_state(
            f"MODO {label}", COLOR_SUCCESS if can_sign else COLOR_MUTED
        )
        self._mode_chip.setToolTip(
            f"Modo activo: {label}. "
            + (
                "Puede firmar y emitir; los topes y el interruptor siguen mandando."
                if can_sign
                else "No firma ni emite: lee, calcula y prepara."
            )
        )

    def set_estimate(self, text: str, *, known: bool = True) -> None:
        """La cifra de la pata que se recibe, o su ausencia."""
        self._want.set_readout(text, bright=known)

    def set_slippage_bps(self, bps: int) -> None:
        """El deslizamiento vigente, en el tooltip del engranaje.

        Se enseña aquí porque es un número que cambia lo que se firma —el mínimo
        que el swap acepta recibir— y comprobarlo no debería costar abrir un
        diálogo. Lo que se pinta es el valor **vivo**, el mismo que usará el
        motor al construir: si dijera otro, sería una etiqueta que miente.
        """
        self._slippage_btn.setToolTip(
            f"Deslizamiento por defecto: {BasisPoints(bps).as_percent():f} %.\n\n"
            "Es la tolerancia con la que se construye el swap: el mínimo que "
            "aceptas recibir. Se configura aquí y se guarda en config.toml."
        )

    def set_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet("")

    def set_status_alert(self, text: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {COLOR_DANGER};")

    def status_text(self) -> str:
        return self._status.text()

    def send_status_text(self) -> str:
        return self._send_status.text()

    def set_segment(self, index: int) -> None:
        self._segment_btns[index].setChecked(True)
        self._show_form(index)

    def current_segment(self) -> int:
        return self._forms.currentIndex()

    def _show_form(self, index: int) -> None:
        self._forms.setCurrentIndex(index)
        self._segment_btns[index].setChecked(True)
        if index == TAB_RECEIVE:
            self.refresh_deposit()
        elif index == TAB_SEND:
            self.refresh_balances()

    # ------------------------------------------------------------------ #
    # Piezas que la página necesita conectar
    # ------------------------------------------------------------------ #
    @property
    def chain(self) -> QComboBox:
        return self._chain

    @property
    def base(self) -> QComboBox:
        return self._base

    @property
    def contra(self) -> QComboBox:
        return self._contra

    @property
    def amount(self) -> QDoubleSpinBox:
        return self._give.amount

    @property
    def unit(self) -> QLabel:
        return self._give.unit

    @property
    def balance(self) -> QLabel:
        return self._give.balance

    @property
    def max_btn(self) -> QPushButton:
        return self._give.max_btn

    @property
    def pair(self) -> QLabel:
        return self._pair

    @property
    def status(self) -> QLabel:
        return self._status

    @property
    def invert_btn(self) -> QPushButton:
        return self._invert_btn

    @property
    def quote_btn(self) -> QPushButton:
        return self._quote_btn

    @property
    def send_btn(self) -> QPushButton:
        return self._send_btn

    @property
    def slippage_btn(self) -> QPushButton:
        return self._slippage_btn


def qr_pixmap(address: str) -> QPixmap:
    """El QR de una dirección, pintado por el mismo camino que el de la cartera.

    Se delega y no se copia: lo que hace legible un QR a un teléfono —el margen de
    silencio sobre fondo blanco— es exactamente lo que se olvida al reimplementarlo,
    y el fallo se descubre justo cuando hace falta que funcione.
    """
    from amigocompora.ui.pages.wallet import qr_pixmap as _qr

    return _qr(address)
