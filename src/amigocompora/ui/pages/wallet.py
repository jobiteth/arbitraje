"""La pestaña de cartera: qué tienes, dónde está y qué se puede hacer con ello.

### Lo que esta pantalla decide, y por qué se decide aquí

Una cartera enseña **una cifra grande** y, debajo, la lista de lo que la compone.
La cifra grande es lo primero que mira todo el mundo y por eso es lo primero que
puede mentir: se lee como «esto es lo que tienes» incluso cuando sólo se pudo
leer siete de ocho redes, o cuando tres tokens no tienen cotización. Esta pantalla
no puede impedir que se lea así, pero sí puede negarse a escribir un número que no
se sostiene: cuando la foto está incompleta, en ese sitio va **qué falta**, no una
suma a la que le falta un trozo. Ver `domain/wallet.py`.

### Los saldos positivos, y por qué sólo ésos

Se enseña lo que tiene fondos. Una lista de treinta tokens con cero no es
información: es ruido que empuja hacia abajo lo único que se venía a ver. Los que
sí están se ordenan por valor, porque el orden natural de una cartera es «lo que
más pesa, arriba», y los que no se pudieron valorar van **igual** —al final, con
su valor en blanco— porque esconderlos sería decir que no existen. Ese es el
mismo error que el total: un token ilíquido no vale cero, vale «no se sabe».

### El buscador, y qué busca

Busca en tres sitios a la vez —símbolo, nombre y **dirección del contrato**— y
sobre la lista completa, no sobre la filtrada: si buscara sólo lo que ya se ve,
pegar una dirección de un token que no está en la lista no encontraría nada, y
ese es justo el caso en el que se pega una dirección. Cuando no encuentra nada
ofrece añadirlo, que es la única respuesta útil que se puede dar a «este token no
está».

### Multi-cartera

La cartera activa es la de la clave configurada, y ésa es la que firma. Se pueden
**leer** otras direcciones —una cartera de sólo lectura, un contrato, la de otra
persona— y por eso el perfil que se lee lleva su propia dirección y su nombre.
Pero las acciones que mueven dinero se apagan en cuanto la cartera mirada no es
la que firma: ofrecer el botón y fallar al pulsarlo sería enseñar a desconfiar de
los botones. La comparación es por dirección y en minúsculas, porque el mismo
texto puede venir con o sin checksum.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.domain.addresses import (
    is_evm_address,
    is_solana_address,
    shorten,
)
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.models import Token
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import EngineKind
from amigocompora.domain.wallet import (
    ChainHoldings,
    TokenHolding,
    WalletKind,
    WalletProfile,
    WalletSnapshot,
)
from amigocompora.engines.catalog import quote_token
from amigocompora.ui.theme import (
    COLOR_MUTED,
    COLOR_SUCCESS,
    COLOR_WARNING,
)
from amigocompora.ui.widgets import (
    Card,
    Field,
    format_amount,
    spawn,
    token_labels,
    tokens_for_chain,
)

#: La cartera que se enseña al abrir si hay clave configurada.
READONLY_WALLET_ID: Final = "observada"
READONLY_WALLET_LABEL: Final = "Cartera observada"

#: Ancho de la matriz del QR, en módulos, sin contar el margen. Un QR de dirección
#: Ethereum es de 29×29 con corrección «M»; se dibuja a 6 px por módulo.
QR_MODULE_PX: Final = 6
QR_QUIET_MODULES: Final = 2


def qr_pixmap(text: str) -> QPixmap:
    """La matriz de un QR, pintada a mano.

    `segno` devuelve la matriz —una tupla de tuplas de ceros y unos— y no una
    imagen, así que se pinta con `QPainter` y no se arrastra Pillow. Se dibuja
    sobre un fondo blanco **explicito** y con el margen de silencio que pide el
    estándar: un QR sin margen sobre un fondo oscuro no lo lee media docena de
    teléfonos, y el fallo se descubre justo cuando hace falta que funcione.
    """
    import segno

    matriz = segno.make(text, error="m").matrix
    lados = len(matriz)
    total = (lados + QR_QUIET_MODULES * 2) * QR_MODULE_PX
    lienzo = QPixmap(total, total)
    lienzo.fill(QColor("white"))

    from PySide6.QtGui import QPainter

    pintor = QPainter(lienzo)
    pintor.setPen(Qt.NoPen)
    pintor.setBrush(QColor("black"))
    for fila, celdas in enumerate(matriz):
        for columna, celda in enumerate(celdas):
            if not celda:
                continue
            pintor.drawRect(
                (columna + QR_QUIET_MODULES) * QR_MODULE_PX,
                (fila + QR_QUIET_MODULES) * QR_MODULE_PX,
                QR_MODULE_PX,
                QR_MODULE_PX,
            )
    pintor.end()
    return lienzo


class DepositDialog(QDialog):
    """Enseñar la dirección y su QR: lo que hace falta para **meter** dinero.

    No tiene botón de aceptar y no es un descuido: no hay nada que aceptar. El
    diálogo existe para copiar y para escanear, así que el único botón cierra.
    Un «Aceptar» invitaría a creer que al pulsarlo pasa algo.

    El QR lleva la dirección **sola**, sin prefijo `ethereum:` ni importe. Un
    prefijo hace que muchas carteras pidan confirmar la red, y aquí la red es una
    elección de esta pantalla: ponerla dentro del código haría que el QR dijera
    una red distinta de la que está seleccionada arriba.
    """

    def __init__(self, address: str, chain_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Depositar en {CHAINS[chain_key].name}")
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        aviso = QLabel(
            f"Envía sólo activos de <b>{CHAINS[chain_key].name}</b> a esta dirección. "
            f"Lo que llegue por otra red <b>no se recupera</b>: no hay a quién "
            f"reclamárselo."
        )
        aviso.setTextFormat(Qt.RichText)
        aviso.setWordWrap(True)
        aviso.setStyleSheet(f"color: {COLOR_WARNING};")
        lay.addWidget(aviso)

        qr = QLabel()
        qr.setPixmap(qr_pixmap(address))
        qr.setAlignment(Qt.AlignCenter)
        qr.setToolTip("Escanea para obtener la dirección")
        lay.addWidget(qr, 0, Qt.AlignCenter)

        self._address = QLineEdit(address)
        self._address.setReadOnly(True)
        self._address.setCursorPosition(0)
        self._address.setMinimumWidth(430)
        lay.addWidget(self._address)

        fila = QHBoxLayout()
        copiar = QPushButton("Copiar dirección")
        copiar.clicked.connect(self._copy)
        fila.addWidget(copiar)
        self._aviso = QLabel("")
        self._aviso.setObjectName("hint")
        fila.addWidget(self._aviso, 1)
        lay.addLayout(fila)

        caja = QDialogButtonBox(QDialogButtonBox.Close)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._address.text())
        self._aviso.setText("Copiada. Se queda en el portapapeles hasta que algo la sustituya.")


class WithdrawDialog(QDialog):
    """La retirada: token, importe y destino.

    El diálogo **no firma**. Recoge lo que hace falta, lo devuelve y el caso de
    uso hace el resto, que es lo que mantiene el orden de las comprobaciones en
    un solo sitio: modo, red, destino, topes, saldo, permiso, firma. Un diálogo
    que llamara a firmar tendría su propia copia de ese orden, y la copia es
    justo lo que se desvía.
    """

    def __init__(
        self,
        tokens: tuple[Token, ...],
        *,
        default: Token | None = None,
        owner: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Retirar fondos")
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.token = QComboBox()
        for etiqueta, token in zip(token_labels(tokens), tokens, strict=True):
            self.token.addItem(etiqueta, token)
        if default is not None:
            for index in range(self.token.count()):
                if self.token.itemData(index) == default:
                    self.token.setCurrentIndex(index)
                    break
        form.addRow("Token", self.token)

        self.amount = QLineEdit()
        self.amount.setPlaceholderText("0.0")
        form.addRow("Importe", self.amount)

        self.recipient = QLineEdit()
        self.recipient.setPlaceholderText("0x…")
        self.recipient.setMinimumWidth(380)
        form.addRow("Destino", self.recipient)

        lay.addLayout(form)

        pista = QLabel(
            f"Sale de <b>{owner}</b>. El destino no puede ser esa misma dirección: "
            f"una transferencia a la propia cartera sólo gasta gas. Y no se puede "
            f"deshacer — revisa la dirección carácter a carácter."
        )
        pista.setTextFormat(Qt.RichText)
        pista.setWordWrap(True)
        pista.setObjectName("hint")
        lay.addWidget(pista)

        caja = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        caja.accepted.connect(self.accept)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def chosen(self) -> tuple[Token, str, str]:
        """El token, el importe en texto y el destino, tal como se escribieron.

        El importe sale como **texto** y no como número: convertirlo aquí sería
        redondear a la escala que elija este diálogo, y el importe de una retirada
        se convierte una sola vez, en el caso de uso, con los decimales del token
        que se está moviendo. Un `QDoubleSpinBox` de seis decimales convierte
        0,0000012 en 0,000001 sin decirlo, y eso es un raw de menos.
        """
        token = self.token.currentData()
        # `currentData()` está declarado como `Any` y devuelve `None` si el
        # desplegable está vacío. No lo está —quien abre este diálogo comprueba
        # antes que hay tokens—, pero comprobarlo **aquí**, en la frontera con
        # Qt, es lo que permite que a partir de esta línea el tipo sea el que
        # dice ser en vez de un `Any` que se propaga hasta el caso de uso.
        if not isinstance(token, Token):
            raise InvalidAmountError(
                "el diálogo de retirada se abrió sin ningún token que retirar"
            )
        return token, self.amount.text().strip(), self.recipient.text().strip()


class WalletPage(QWidget):
    """Cartera: saldos por red, con QR para depositar y formulario para retirar."""

    #: Pide a la pestaña de swap que prepare un par con este token. La señal lleva
    #: el token y la red porque el swap tiene su propio desplegable de red y hay
    #: que ponerlo en la misma antes de tocar las patas.
    swap_requested = Signal(object)

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._snapshot: WalletSnapshot | None = None
        self._tokens: list[Token] = list(container.token_store.load())
        self._full_figures = False
        self._profile: WalletProfile | None = None
        #: Las filas de la última lectura, **con** las que están a cero. Se
        #: guardan enteras y se filtran al pintar: si el filtro se aplicara aquí,
        #: desmarcar «Sólo con saldo» no tendría nada que volver a enseñar y el
        #: botón parecería roto.
        self._rows: list[tuple[ChainHoldings, TokenHolding]] = []
        #: Número de la lectura en curso. Ver `refresh`: cada lectura se lleva el
        #: suyo y lo que llega tarde se reconoce por no ser el último.
        self._read_id = 0

        # Un layout normal, **sin** armazón que se desplace, y no por descuido:
        # esta página ya no es una pestaña. La ventana la mete dentro del scroll de
        # la pestaña de swap, y un `QScrollArea` dentro de otro no desplaza dos
        # veces —el de dentro mide lo que le da el de fuera y recorta su propio
        # contenido, que es exactamente el defecto que se venía a arreglar—. Quien
        # desplaza es el de fuera; aquí sólo se apila.
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(self._build_header())
        lay.addWidget(self._build_tokens())
        lay.addStretch(1)

        self._chain.currentIndexChanged.connect(self._on_chain_changed)
        self._search.textChanged.connect(self._repaint_rows)
        self._only_positive.toggled.connect(self._repaint_rows)
        # La cartera con la que se abre es la que abre la clave configurada, si la
        # hay. Sin esto la pestaña arranca en blanco teniendo cartera: el usuario
        # tendría que pulsar «Volver a la mía» para ver unos saldos que la
        # aplicación ya sabe de quién son.
        mia = container.keys.address()
        if mia:
            self._address.setText(mia)
        self.refresh()

    # ------------------------------------------------------------------ #
    # Cabecera: la cartera, el total y las acciones
    # ------------------------------------------------------------------ #
    def _build_header(self) -> QWidget:
        card = Card("Cartera")

        #: El estado de la lectura, en la cabecera: es lo que decide si el total
        #: de abajo se puede creer.
        self._state = QLabel("")
        self._state.setObjectName("hint")
        card.header.addWidget(self._state)

        top = QHBoxLayout()
        self._address = QLineEdit("")
        self._address.setReadOnly(True)
        self._address.setCursorPosition(0)
        self._address.setMinimumWidth(380)
        self._address.setToolTip(
            "La dirección que se lee. Con clave configurada es la cartera que firma; "
            "si se mira otra, las acciones que mueven dinero se apagan."
        )
        address_field = Field("DIRECCIÓN")
        address_field.add(self._address, 1)

        self._lookup_address = QPushButton("Mirar otra…")
        self._lookup_address.setObjectName("secondary")
        self._lookup_address.setToolTip(
            "Lee los saldos de una dirección que no es la que firma —una cartera de "
            "sólo lectura, un contrato, la de otra persona—. Sin clave privada no "
            "se puede firmar desde ella, así que depositar sigue estando disponible "
            "y retirar no."
        )
        self._lookup_address.clicked.connect(self._on_lookup_address)
        address_field.add(self._lookup_address)

        self._use_mine = QPushButton("Volver a la mía")
        self._use_mine.setObjectName("secondary")
        self._use_mine.clicked.connect(self._on_use_mine)

        self._chain = QComboBox()
        self._chain.setMinimumWidth(190)
        chain_field = Field("RED")
        chain_field.add(self._chain, 1)

        top.addWidget(address_field, 2)
        top.addWidget(self._use_mine, 0, Qt.AlignBottom)
        top.addWidget(chain_field, 1)
        card.add_row(top)

        # El total, en grande. Es la cifra que se lee primero y la que puede
        # mentir, así que debajo lleva siempre su explicación.
        total_row = QHBoxLayout()
        self._total = QLabel("—")
        self._total.setObjectName("bigNumber")
        self._total.setStyleSheet(f"color: {COLOR_MUTED};")
        self._total.setToolTip(
            "Lo que suman las posiciones valoradas en la stablecoin de cada red. "
            "No es el patrimonio si la foto está incompleta o hay posiciones sin "
            "cotización."
        )
        total_row.addWidget(self._total)
        self._total_hint = QLabel("")
        self._total_hint.setObjectName("hint")
        self._total_hint.setWordWrap(True)
        total_row.addWidget(self._total_hint, 1)
        card.add_row(total_row)

        actions = QHBoxLayout()
        self._deposit_btn = QPushButton("Depositar")
        self._deposit_btn.setToolTip(
            "Muestra la dirección de esta red en texto y en código QR, y permite "
            "copiarla. No hay nada que firmar: es la dirección que ya se está usando."
        )
        self._deposit_btn.clicked.connect(self._on_deposit)

        self._withdraw_btn = QPushButton("Retirar")
        self._withdraw_btn.setObjectName("secondary")
        self._withdraw_btn.setToolTip(
            "Saca un token de la cartera hacia otra dirección. Firma y emite: es "
            "una operación real e irreversible."
        )
        self._withdraw_btn.clicked.connect(self._on_withdraw)

        self._refresh_btn = QPushButton("Actualizar saldos")
        self._refresh_btn.setObjectName("secondary")
        self._refresh_btn.clicked.connect(self.refresh)

        self._full_btn = QPushButton("Ver cifras completas")
        self._full_btn.setObjectName("secondary")
        self._full_btn.setCheckable(True)
        self._full_btn.setToolTip(
            "Alterna entre el saldo recortado —el que se lee— y el número entero "
            "con todos sus decimales. El recorte es sólo de la pantalla: lo que se "
            "firma sale de la cifra completa."
        )
        self._full_btn.toggled.connect(self._on_full_toggled)

        actions.addWidget(self._deposit_btn)
        actions.addWidget(self._withdraw_btn)
        actions.addStretch(1)
        actions.addWidget(self._full_btn)
        actions.addWidget(self._refresh_btn)
        card.add_row(actions)

        self._notice = QLabel("")
        self._notice.setObjectName("hint")
        self._notice.setWordWrap(True)
        card.add_row(self._wrap(self._notice))
        return card

    @staticmethod
    def _wrap(widget: QWidget) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.addWidget(widget, 1)
        return fila

    # ------------------------------------------------------------------ #
    # La lista de tokens
    # ------------------------------------------------------------------ #
    def _build_tokens(self) -> QWidget:
        card = Card(
            "Tokens con saldo",
            subtitle="— sólo lo que tiene fondos; el buscador mira la lista entera",
        )

        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar por nombre, símbolo o dirección…")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(320)
        self._search.setToolTip(
            "Filtra la lista por símbolo, por nombre o por la dirección del "
            "contrato. Si lo que pegas no está en la lista, se ofrece añadirlo."
        )
        card.header.addWidget(self._search)

        self._add_btn = QPushButton("+ Añadir token")
        self._add_btn.setObjectName("secondary")
        self._add_btn.setToolTip(
            "Añade un token por la dirección de su contrato. El símbolo y los "
            "decimales se leen de la cadena, no se piden a mano, y el token queda "
            "guardado para la próxima vez."
        )
        self._add_btn.clicked.connect(self._on_add_token)
        card.header.addWidget(self._add_btn)

        self._only_positive = QCheckBox("Sólo con saldo")
        self._only_positive.setChecked(True)
        self._only_positive.setToolTip(
            "Desmárcalo para ver también los tokens de la lista que están a cero. "
            "Sirve para comprobar que un token está en la cartera aunque ahora no "
            "tenga nada."
        )
        card.header.addWidget(self._only_positive)

        self._table = QTableWidget(0, 6)
        # La última columna no lleva título: es el botón de cambiar, y un
        # encabezado encima de un botón sólo ocupa sitio. Va la última para que
        # `columnCount()` siga creciendo por la derecha y las cinco columnas de
        # dato no se muevan de sitio.
        self._table.setHorizontalHeaderLabels(
            ["Token", "Saldo", "Valor", "Red", "Contrato", ""]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        cabecera = self._table.horizontalHeader()
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        cabecera.setSectionResizeMode(1, QHeaderView.Stretch)
        cabecera.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        cabecera.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        cabecera.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        cabecera.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        self._table.setMinimumHeight(200)
        card.body().addWidget(self._table)

        self._empty = QLabel("")
        self._empty.setObjectName("hint")
        self._empty.setWordWrap(True)
        self._empty.setVisible(False)
        card.body().addWidget(self._empty)

        row = QHBoxLayout()
        self._row_actions = QLabel("")
        self._row_actions.setObjectName("hint")
        row.addWidget(self._row_actions, 1)

        self._swap_btn = QPushButton("Intercambiar este token")
        self._swap_btn.setObjectName("secondary")
        self._swap_btn.setEnabled(False)
        self._swap_btn.setToolTip(
            "Lleva este token a la tarjeta de conversión, en su red, para cotizar "
            "una operación con él sin volver a buscarlo en la lista."
        )
        self._swap_btn.clicked.connect(self._on_swap_selected)

        self._filter_btn = QPushButton("Buscar este token")
        self._filter_btn.setObjectName("secondary")
        self._filter_btn.setEnabled(False)
        self._filter_btn.setToolTip(
            "Pone su dirección en el buscador: útil para ver de un vistazo si el "
            "token que se acaba de añadir aparece en la lista."
        )
        self._filter_btn.clicked.connect(self._on_filter_selected)

        row.addWidget(self._swap_btn)
        row.addWidget(self._filter_btn)
        card.add_row(row)
        return card

    # ------------------------------------------------------------------ #
    # Leer la cartera
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """Vuelve a leer los saldos de la cartera que se esté mirando.

        Se leen primero los **saldos** y se pintan, y la valoración va después en
        su propia tarea: valorar es una cotización por token con fondos, así que
        esperar a tenerlo todo dejaría la pantalla vacía durante la parte lenta de
        una pregunta que el usuario no hizo. Con los dos pasos separados, la lista
        aparece en cuanto llegan los saldos y los importes se rellenan solos.

        ### Cada lectura se lleva su número, y lo que llega tarde se calla

        Dos `refresh()` seguidos lanzan dos lecturas que compiten, y sin nada que
        las ordene pinta la que **termina** última, que casi siempre es la primera
        en empezar: al abrir la pestaña ya hay una lectura en marcha —la de la
        clave configurada— y escribir otra dirección encima la deja corriendo.

        Está medido, y es peor de lo que suena: la lectura de la clave (ocho
        redes, con su valoración) tardó **9,8 s** y la de una sola red de Solana,
        **1,4 s**. La de Solana pintaba primero, correctamente, y a los ocho
        segundos la otra la pisaba. La pantalla acababa enseñando los saldos de la
        cartera del usuario —19,49 POL, 0,0000025 ETH— **bajo la dirección que él
        acababa de escribir**, con el contador de redes de la lectura vieja al
        lado. Un saldo bajo la dirección equivocada no es un desliz de pintado: es
        la única clase de error que aquí no se puede permitir, porque el usuario
        decide si tiene dinero mirando eso.

        El remedio es un número por lectura y no cancelar la tarea. Cancelar
        ahorraría las peticiones que quedan, y es tentador con un nodo de Solana
        racionado por ventana, pero interrumpir a media lectura deja el `gather`
        de `ReadWallet` a medio recoger y convierte un problema de pintado en uno
        de estado; lo que llega tarde se descarta al llegar, que es donde no
        cuesta nada.
        """
        self._read_id += 1
        lectura = self._read_id

        direccion = self._address.text().strip()
        if not direccion:
            self._notice.setText(
                "No hay ninguna cartera configurada. Pon la clave privada en la "
                "pestaña de Motores, en Credenciales, o mira otra dirección con "
                "«Mirar otra…»."
            )
            self._clear()
            return

        try:
            profile = self._profile_for(direccion)
        except InvalidAmountError as error:
            # Una dirección mal formada —el caso de un pegado a medias— se dice y
            # se deja la pantalla en blanco. Dejarla subir sería peor que un
            # mensaje vacío: esto corre dentro de un manejador de señal de Qt, y
            # una excepción sin recoger ahí no la ve nadie —no hay un `try` por
            # encima— y en las versiones que la propagan se lleva por delante la
            # aplicación entera. Pegar mal una dirección no puede costar la
            # aplicación.
            self._clear()
            self._notice.setText(str(error))
            return
        self._profile = profile
        self._refresh_buttons()
        self._refresh_chain_combo(profile)
        self._state.setText("Leyendo saldos…")
        self._refresh_btn.setEnabled(False)
        spawn(self._do_read(profile, lectura))

    def _sigue_vigente(self, lectura: int) -> bool:
        """Si esa lectura es todavía la que manda en la pantalla.

        Se pregunta **después de cada espera** y no una sola vez al empezar: lo
        que se descarta no es la lectura, es su derecho a pintar, y ese derecho se
        puede perder en cualquier momento —basta con que el usuario escriba otra
        dirección mientras la valoración está en vuelo.
        """
        return lectura == self._read_id

    async def _do_read(self, profile: WalletProfile, lectura: int) -> None:
        try:
            snapshot = await self._container.read_wallet(profile)
        except Exception as error:
            if not self._sigue_vigente(lectura):
                return
            self._state.setText("")
            self._notice.setText(f"No se pudo leer la cartera: {error}")
            self._refresh_btn.setEnabled(True)
            return
        if not self._sigue_vigente(lectura):
            return
        self._snapshot = snapshot
        self._refresh_btn.setEnabled(True)
        self._paint(snapshot)
        # Y ahora los importes, sin volver a tocar los nodos: `ValueWallet`
        # recibe la foto ya leída justamente para esto.
        self._state.setText("Valorando…")
        try:
            valued = await self._container.value_wallet(snapshot)
        except Exception as error:
            if not self._sigue_vigente(lectura):
                return
            self._state.setText("")
            self._notice.setText(
                f"Los saldos están, pero no se pudieron valorar: {error}. Las "
                f"cantidades son las de la cadena."
            )
            return
        if not self._sigue_vigente(lectura):
            return
        self._snapshot = valued
        self._paint(valued)

    def _clear(self) -> None:
        """Deja la lista y el total vacíos, sin tocar el motivo.

        El motivo lo escribe quien llama —«no hay ninguna cartera configurada»,
        «esa dirección no tiene forma de dirección»—, porque es lo único que
        distingue los dos casos y los dos acaban con la misma pantalla en blanco.

        `_rows` se vacía además de la tabla, y no es lo mismo: el buscador y el
        interruptor repintan desde `_rows`, así que dejarlas vivas haría que el
        primer clic en cualquiera de los dos resucitara las filas de la cartera
        anterior bajo el nombre de la nueva.

        El botón de releer se vuelve a encender aquí, y hace falta: quien lo apagó
        fue la lectura que estaba en marcha, y si esa lectura queda anulada por
        haberse pedido otra cosa, ya no lo encenderá nadie. Sin esta línea, pegar
        una dirección a medias con una lectura en vuelo dejaba el botón apagado
        para siempre.
        """
        self._profile = None
        self._snapshot = None
        self._rows = []
        self._table.setRowCount(0)
        self._total.setText("—")
        self._total_hint.setText("")
        self._state.setText("")
        self._refresh_btn.setEnabled(True)
        self._refresh_buttons()

    def _profile_for(self, address: str) -> WalletProfile:
        """El perfil que se lee: la cartera que firma, o la que se pidió mirar.

        La familia se deduce por la **forma** de la dirección y no se pregunta: un
        `0x…` de 40 dígitos es EVM y un base58 de 32 bytes es Solana, y una
        dirección que no sea ninguna de las dos lanza aquí, que es donde se puede
        explicar.
        """
        if is_evm_address(address):
            kind = WalletKind.EVM
        elif is_solana_address(address):
            kind = WalletKind.SOLANA
        else:
            raise InvalidAmountError(
                f"«{shorten(address)}» no tiene forma de dirección EVM (0x y 40 "
                f"dígitos hexadecimales) ni de pubkey de Solana (base58 de 32 bytes)."
            )
        mia = (self._container.keys.address() or "").lower()
        es_mia = bool(mia) and mia == address.lower()
        return WalletProfile(
            wallet_id="principal" if es_mia else READONLY_WALLET_ID,
            label="Mi cartera" if es_mia else READONLY_WALLET_LABEL,
            kind=kind,
            address=address,
        )

    def _refresh_chain_combo(self, profile: WalletProfile) -> None:
        """Las redes del desplegable: las que esta cartera puede leer, y «Todas».

        Sólo las de su familia, y ni una más: ofrecer Polygon para una cartera de
        Solana sería ofrecer una consulta que siempre falla.
        """
        previa = self._chain.currentData()
        self._chain.blockSignals(True)
        self._chain.clear()
        self._chain.addItem("Todas las redes", None)
        for key in profile.chains_of():
            if key in self._engine_chains():
                self._chain.addItem(f"{CHAINS[key].name}", key)
        self._chain.blockSignals(False)
        for index in range(self._chain.count()):
            if self._chain.itemData(index) == previa:
                self._chain.setCurrentIndex(index)
                return

    def _engine_chains(self) -> frozenset[str]:
        """Las redes que el motor de cartera declara saber leer.

        Se leen del manifiesto y no del catálogo porque la diferencia está medida:
        el catálogo tiene once redes y el motor declara nueve. Ofrecer las once
        enseñaría dos que fallan siempre, y una pantalla que ofrece algo que nunca
        funciona es una pantalla que enseña a no creerla.
        """
        try:
            engine = self._container.registry.active_or_none(EngineKind.WALLET)
        except Exception:  # pragma: no cover - el registro no lanza hoy
            return frozenset()
        if engine is None:
            return frozenset()
        return frozenset(engine.manifest.wallet_chains)

    def _on_chain_changed(self) -> None:
        self._repaint_rows()

    # ------------------------------------------------------------------ #
    # Pintar
    # ------------------------------------------------------------------ #
    def _paint(self, snapshot: WalletSnapshot) -> None:
        self._rows = self._rows_of(snapshot)
        self._repaint_rows()
        self._paint_total(snapshot)
        self._paint_state(snapshot)
        self._paint_notice(snapshot)

    def _rows_of(self, snapshot: WalletSnapshot) -> list[tuple[ChainHoldings, TokenHolding]]:
        """Todas las filas de la lectura, ordenadas por lo que pesan.

        Orden estable y con criterio, porque una lista que se reordena sola entre
        dos lecturas es una lista en la que no se puede buscar nada con el dedo:
        primero las que tienen valor, de mayor a menor, y después las que no se
        pudieron valorar. Las que están a cero van al final —y se esconden al
        pintar— pero **se conservan aquí**, porque el interruptor que las enseña
        no puede reconstruir lo que no se guardó.
        """
        filas = [
            (cadena, holding)
            for cadena in snapshot.chains
            if not cadena.failed
            for holding in cadena.holdings
        ]
        filas.sort(
            key=lambda par: (
                par[1].is_empty,
                par[1].value_in_reference is None,
                -(
                    par[1].value_in_reference.as_decimal()
                    if par[1].value_in_reference is not None
                    else Decimal(0)
                ),
                par[1].token.symbol,
            )
        )
        return filas

    def _repaint_rows(self) -> None:
        """Pinta las filas que pasan los tres filtros: red, saldo y buscador.

        Los tres se aplican aquí y no en la lectura porque los tres se pueden
        cambiar sin volver a preguntar a un nodo, y volver a preguntar por un
        cambio de filtro sería gastar ocho llamadas de red para esconder una fila.
        """
        filtro = self._search.text().strip().lower()
        red = self._chain.currentData()
        solo_con_saldo = self._only_positive.isChecked()

        visibles = [
            (cadena, holding)
            for cadena, holding in self._rows
            if (red is None or cadena.chain == red)
            and (not solo_con_saldo or not holding.is_empty)
            and self._matches(holding, filtro)
        ]

        self._table.setRowCount(len(visibles))
        for indice, (cadena, holding) in enumerate(visibles):
            self._fill_row(indice, cadena, holding)

        vacio = not visibles and bool(self._rows)
        self._empty.setVisible(vacio)
        if vacio:
            self._empty.setText(self._empty_text(filtro, red))

    def _empty_text(self, filtro: str, red: str | None) -> str:
        if filtro and self._looks_like_contract(filtro):
            return (
                f"«{filtro}» no aparece con saldo. Si es la dirección de un token "
                f"que tienes, añádelo con «+ Añadir token»: la lista del catálogo es "
                f"finita y un token nuevo no puede estar en ella."
            )
        if filtro:
            return (
                f"Nada con saldo coincide con «{filtro}». El buscador mira la lista "
                f"completa, así que lo que no aparece tampoco está en el catálogo: "
                f"añádelo por su dirección."
            )
        if self._only_positive.isChecked():
            return (
                "Esta cartera está a cero en todas las redes que se leyeron. "
                "Desmarca «Sólo con saldo» para ver la lista de tokens vigilados "
                "—que es también la lista de lo que se puede llegar a tener—."
            )
        if red is not None:
            return (
                f"No hay saldos en {CHAINS[red].name} para esta dirección. Cambia de "
                f"red arriba, o mira «Todas las redes»."
            )
        return "Esta cartera no tiene saldo en ninguna de las redes que se pudieron leer."

    @staticmethod
    def _looks_like_contract(texto: str) -> bool:
        return texto.startswith("0x") and len(texto) == 42

    @staticmethod
    def _matches(holding: TokenHolding, filtro: str) -> bool:
        """Si el token pasa el filtro: símbolo, nombre cualificado o **contrato**.

        La dirección se busca en minúsculas y sin exigir el `0x` porque es como se
        pega la mitad de las veces, y porque comparar por el texto exacto fallaría
        con un checksum distinto — y una búsqueda que falla por mayúsculas es una
        búsqueda que parece decir «no lo tienes».

        No usa el estado de la pantalla y por eso es estático: la decisión de qué
        coincide con qué es una función de dos datos, y tenerla colgada de la
        instancia haría falta construir una ventana para comprobarla.
        """
        if not filtro:
            return True
        token = holding.token
        aguja = filtro.removeprefix("0x")
        candidatos = {token.symbol.lower(), token.qualified_symbol.lower()}
        if token.address:
            candidatos.add(token.address.lower())
            candidatos.add(token.address.lower().removeprefix("0x"))
        return any(aguja in candidato for candidato in candidatos)

    def _fill_row(self, indice: int, cadena: ChainHoldings, holding: TokenHolding) -> None:
        token = holding.token
        nombre = QTableWidgetItem(token.qualified_symbol)
        nombre.setToolTip(
            f"{token.symbol} en {CHAINS[cadena.chain].name}, {token.decimals} decimales"
            + ("" if token.address else " (moneda nativa de la red)")
        )
        self._table.setItem(indice, 0, nombre)

        cantidad = QTableWidgetItem(format_amount(holding.as_decimal(), full=self._full_figures))
        # El tooltip lleva siempre la cifra **entera**: el recorte es de la
        # columna, y tener que pulsar un botón para comprobar un saldo sería
        # esconder un dato que ya está calculado.
        cantidad.setToolTip(f"{holding.as_decimal():f} {token.symbol}")
        cantidad.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(indice, 1, cantidad)

        if holding.is_empty:
            # Cero no es «no se sabe». La valoración **no pregunta** por un saldo
            # vacío —no hay nada que valorar, y `ValueWallet` los deja fuera del
            # presupuesto a propósito—, así que pintar aquí «sin cotización»
            # contaba un fallo de precios que no ocurrió, y al lado de un 0 se
            # lee como que la cotización no llega. La cifra sí se puede afirmar
            # sin consultar a nadie: cero por cualquier precio es cero.
            unidad = quote_token(cadena.chain)
            valor = QTableWidgetItem(
                f"0 {unidad.symbol}" if unidad is not None else "0"
            )
            valor.setForeground(QColor(COLOR_MUTED))
            valor.setToolTip(
                "Esta posición está a cero, así que no hay nada que valorar y no se "
                "preguntó a ningún motor por su precio. Vale cero exactamente, no "
                "«no se sabe»."
            )
        elif holding.value_in_reference is None:
            valor = QTableWidgetItem("sin cotización")
            valor.setForeground(QColor(COLOR_MUTED))
            valor.setToolTip(
                "Esta posición tiene fondos y no se le pudo poner precio: ningún "
                "motor activo cotiza este token contra la stablecoin de su red. No "
                "vale cero — vale «no se sabe», y por eso no entra en el total."
            )
        else:
            referencia = holding.value_in_reference
            valor = QTableWidgetItem(
                f"{format_amount(referencia.as_decimal(), full=self._full_figures)} "
                f"{referencia.symbol}"
            )
            valor.setToolTip(f"{referencia.as_decimal():f} {referencia.symbol}")
        valor.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(indice, 2, valor)

        self._table.setItem(indice, 3, QTableWidgetItem(CHAINS[cadena.chain].name))
        contrato = QTableWidgetItem(shorten(token.address) if token.address else "—")
        if token.address:
            contrato.setToolTip(token.address)
            # La dirección completa, en el texto del widget y no sólo en el
            # tooltip: es el dato con el que se distingue el USDC nativo del
            # puenteado, que publican el mismo símbolo.
            contrato.setData(Qt.UserRole, token.address)
        self._table.setItem(indice, 4, contrato)
        self._table.setCellWidget(indice, 5, self._row_swap_button(holding))

    def _row_swap_button(self, holding: TokenHolding) -> QPushButton:
        """El botón ⇄ de una fila: intercambiar **ese** token.

        Es la diferencia entre una lista que se mira y una lista con la que se
        opera. Antes había que seleccionar la fila, bajar a la fila de acciones,
        pulsar «Intercambiar este token» y **cambiar de pestaña**; cuatro gestos
        para llevar un token a una tarjeta que ahora está dos centímetros más
        abajo. El botón lleva su token dentro y no lee la selección, porque pulsar
        un widget dentro de una celda no selecciona la fila: leerla aquí mandaría
        el token de la última fila que se tocó, y eso se paga firmando lo que no
        se quería.
        """
        token = holding.token
        btn = QPushButton("⇄")
        btn.setObjectName("rowAction")
        btn.setFixedWidth(30)
        btn.setEnabled(token.chain in CHAINS)
        btn.setToolTip(
            f"Llevar {token.symbol} a la tarjeta de conversión, en "
            f"{CHAINS[token.chain].name}."
            if token.chain in CHAINS
            else f"{CHAINS[token.chain].name} no se puede convertir todavía."
        )
        btn.clicked.connect(lambda _=False, t=token: self.swap_requested.emit(t))
        return btn

    def _paint_total(self, snapshot: WalletSnapshot) -> None:
        """El total, o la verdad de por qué no hay total.

        Aquí está la decisión de toda la pantalla. Un número que parece un
        patrimonio y no lo es es peor que ningún número: se copia, se compara y
        se decide con él. Así que cuando la foto no se puede afirmar, en el sitio
        del total va qué falta.
        """
        total = snapshot.total_in_reference
        if total is not None:
            self._total.setText(f"{format_amount(total)} USDC")
            self._total.setStyleSheet(f"color: {COLOR_SUCCESS};")
            self._total_hint.setText(
                "Suma de todas las redes leídas, en la stablecoin de cada una. "
                "Todas las posiciones con fondos están valoradas."
            )
            return

        self._total.setText("—")
        self._total.setStyleSheet(f"color: {COLOR_MUTED};")
        motivos: list[str] = []
        if snapshot.failed_chains:
            # Con el **nombre** de la red y no con su clave interna: «no se pudo
            # leer polygon» es una línea de registro, y quien lee esto está
            # mirando una pantalla.
            motivos.append(
                "no se pudieron leer "
                + ", ".join(CHAINS[key].name for key in snapshot.failed_chains)
            )
        if snapshot.partial_chains:
            motivos.append(
                "se leyeron a medias "
                + ", ".join(CHAINS[key].name for key in snapshot.partial_chains)
            )
        sin_valorar = len(snapshot.unvalued_positions)
        if sin_valorar:
            motivos.append(
                f"{sin_valorar} posición(es) con fondos sin cotización"
            )
        if motivos:
            self._total_hint.setText(
                "No hay un total que se pueda afirmar: " + "; ".join(motivos) + ". "
                "Un total al que le falta una red o un token dice que tienes menos "
                "de lo que tienes, así que no se enseña."
            )
        else:
            self._total_hint.setText("No hay nada que sumar todavía.")

    def _paint_state(self, snapshot: WalletSnapshot) -> None:
        leidas = len(snapshot.read_chains)
        fallidas = len(snapshot.failed_chains)
        partes = [f"{leidas} red(es) leída(s)"]
        if fallidas:
            partes.append(f"{fallidas} sin leer")
        partes.append(f"a las {snapshot.read_at:%H:%M:%S}")
        self._state.setText(" · ".join(partes))
        self._state.setStyleSheet(
            f"color: {COLOR_WARNING};" if fallidas or snapshot.partial_chains else ""
        )

    def _paint_notice(self, snapshot: WalletSnapshot) -> None:
        """Lo que falta, dicho entero y en el mismo sitio donde está el total.

        Se listan las redes con su motivo y las posiciones sin valorar con su
        token: «faltan 3 posiciones» no permite hacer nada, y «POL en Polygon no
        tiene cotización» sí.
        """
        lineas: list[str] = []
        for cadena in snapshot.chains:
            if cadena.error is not None:
                lineas.append(f"✗ {CHAINS[cadena.chain].name}: {cadena.error}")
                continue
            for motivo in cadena.missing:
                lineas.append(f"⚠ {CHAINS[cadena.chain].name}: {motivo}")

        sin_valorar = snapshot.unvalued_positions
        if sin_valorar:
            nombres = ", ".join(
                sorted({f"{h.token.symbol} ({h.token.chain})" for h in sin_valorar})
            )
            lineas.append(f"⚠ sin cotización: {nombres}")

        self._notice.setText("\n".join(lineas))
        self._notice.setVisible(bool(lineas))

    # ------------------------------------------------------------------ #
    # Acciones
    # ------------------------------------------------------------------ #
    def _refresh_buttons(self) -> None:
        """Quién puede hacer qué, y por qué no.

        Depositar funciona siempre: es enseñar una dirección pública. Retirar
        necesita tres cosas —una clave que firme, que la cartera mirada **sea** la
        que esa clave abre, y una red EVM— y las tres se comprueban al pintar, no
        al pulsar. Un botón que se puede pulsar y falla enseña a desconfiar de los
        botones; uno apagado con su motivo al lado enseña qué falta.
        """
        perfil = self._profile
        if perfil is None:
            self._deposit_btn.setEnabled(False)
            self._withdraw_btn.setEnabled(False)
            self._withdraw_btn.setToolTip("No hay ninguna cartera que mirar.")
            return

        self._deposit_btn.setEnabled(True)

        if not self._container.keys.available():
            self._withdraw_btn.setEnabled(False)
            self._withdraw_btn.setToolTip(
                "No hay ninguna clave privada configurada, así que no hay desde "
                "dónde retirar. Ponla en la pestaña de Motores, en Credenciales."
            )
            return

        # La familia **antes** que la dirección, y el orden no es indiferente: una
        # cartera de Solana no se puede retirar desde aquí ni con la clave
        # correcta, así que decirle a quien la mira que «no es la que firma» sería
        # mandarlo a cambiar de clave para volver a chocar con lo mismo.
        if perfil.kind is WalletKind.SOLANA:
            self._withdraw_btn.setEnabled(False)
            self._withdraw_btn.setToolTip(
                "Retirar en Solana todavía no está implementado: la transacción se "
                "construye de otra forma y la comisión se paga en otra unidad. La "
                "pestaña de swap sí cotiza en Solana."
            )
            return

        mia = (self._container.keys.address() or "").lower()
        if mia != perfil.address.lower():
            self._withdraw_btn.setEnabled(False)
            self._withdraw_btn.setToolTip(
                f"Estás mirando {shorten(perfil.address)}, que no es la cartera que "
                f"firma ({shorten(mia)}). Se puede depositar —una dirección pública "
                f"no necesita clave— pero no retirar: firmar desde otra cartera "
                f"mandaría el dinero desde donde no estás mirando."
            )
            return

        self._withdraw_btn.setEnabled(True)
        self._withdraw_btn.setToolTip(
            "Saca un token de la cartera hacia otra dirección. Firma y emite: es "
            "una operación real e irreversible."
        )

    def _selected_holding(self) -> TokenHolding | None:
        fila = self._table.currentRow()
        if fila < 0:
            return None
        item = self._table.item(fila, 0)
        if item is None:
            return None
        # La identidad del token se guarda en el `UserRole` de la columna del
        # contrato, que es la única que no cambia de texto al repintar. Buscarla
        # por el texto de la celda funcionaría hasta que dos tokens compartieran
        # símbolo —y en Polygon lo hacen—.
        contrato = self._table.item(fila, 4)
        red = self._table.item(fila, 3)
        if contrato is None or red is None:
            return None
        direccion = contrato.data(Qt.UserRole)
        cadena = next((key for key in CHAINS if CHAINS[key].name == red.text()), None)
        if cadena is None:
            return None
        for cadena_leida, holding in self._rows:
            if cadena_leida.chain != cadena:
                continue
            if holding.token.address == direccion:
                return holding
        return None

    def _on_row_selected(self) -> None:
        holding = self._selected_holding()
        if holding is None:
            self._row_actions.setText("")
            self._swap_btn.setEnabled(False)
            self._filter_btn.setEnabled(False)
            return
        self._row_actions.setText(
            f"{holding.token.symbol} en {CHAINS[holding.token.chain].name}: "
            f"{holding.as_decimal():f} — valorado en "
            + (
                f"{holding.value_in_reference.as_decimal():f} "
                f"{holding.value_in_reference.symbol}"
                if holding.value_in_reference is not None
                else "nada, no hay cotización"
            )
        )
        self._swap_btn.setEnabled(holding.token.chain in CHAINS)
        self._filter_btn.setEnabled(True)

    def _on_swap_selected(self) -> None:
        holding = self._selected_holding()
        if holding is None:
            return
        self.swap_requested.emit(holding.token)

    def _on_filter_selected(self) -> None:
        holding = self._selected_holding()
        if holding is None or not holding.token.address:
            self._search.setText(holding.token.symbol if holding else "")
            return
        self._search.setText(holding.token.address)

    def _on_full_toggled(self, activo: bool) -> None:
        self._full_figures = activo
        self._full_btn.setText("Ver cifras recortadas" if activo else "Ver cifras completas")
        self._repaint_rows()

    def _on_deposit(self) -> None:
        perfil = self._profile
        if perfil is None:
            return
        red = self._chain.currentData()
        if red is None:
            red = next(iter(perfil.chains_of()), None)
        if red is None:
            self._notice.setText("Esta cartera no cubre ninguna red del catálogo.")
            return
        DepositDialog(perfil.address, red, self).exec()

    def _on_withdraw(self) -> None:
        """Recoge el formulario y llama al caso de uso. Sin atajos.

        El diálogo no firma ni comprueba topes: devuelve lo que el usuario
        escribió y el caso de uso hace el resto en su orden —modo, red, destino,
        topes, saldo, permiso, firma—. Duplicar aquí una parte de ese orden sería
        tener dos versiones de la misma regla, y la que se desvía es siempre la
        que no se ejecuta.
        """
        perfil = self._profile
        if perfil is None:
            return
        red = self._chain.currentData()
        cadena = red or (perfil.chains_of()[0] if perfil.chains_of() else None)
        if cadena is None:
            return
        tokens = tokens_for_chain(self._container.token_store, cadena)
        if not tokens:
            self._notice.setText(f"No hay tokens conocidos en {CHAINS[cadena].name}.")
            return

        seleccionado = self._selected_holding()
        por_defecto = (
            seleccionado.token
            if seleccionado is not None and seleccionado.token.chain == cadena
            else None
        )
        dialogo = WithdrawDialog(tokens, default=por_defecto, owner=perfil.address, parent=self)
        if dialogo.exec() != QDialog.Accepted:
            return
        token, texto, destino = dialogo.chosen()
        try:
            amount = token.amount(texto)
        except Exception as error:
            self._notice.setText(f"Importe no válido: {error}")
            return
        self._notice.setText(f"Retirando {amount} de {token.symbol}…")
        self._withdraw_btn.setEnabled(False)
        spawn(self._do_withdraw(token, amount, destino))

    async def _do_withdraw(self, token: Token, amount: TokenAmount, recipient: str) -> None:
        try:
            receipt = await self._container.withdraw_funds(token, amount, recipient=recipient)
        except Exception as error:
            # El motivo va entero a la pantalla. Un caso de uso que explica por
            # qué no firma —«no se pudo valorar», «la credencial cambió», «no
            # cabe el gas»— pierde todo su valor si la interfaz lo resume en
            # «error».
            self._notice.setText(f"No se retiró nada: {error}")
            return
        finally:
            self._refresh_buttons()
        self._notice.setText(
            f"Retirada emitida: {amount} {token.symbol} → {shorten(recipient)}. "
            f"Hash {receipt.tx_hash}."
        )
        self.refresh()

    def _on_lookup_address(self) -> None:
        direccion, aceptado = QInputDialog.getText(
            self,
            "Mirar otra cartera",
            "Dirección a consultar (EVM «0x…» o Solana en base58):\n"
            "(se leen sus saldos; sin su clave no se puede firmar desde ella)",
        )
        if not aceptado or not direccion.strip():
            return
        self._address.setText(direccion.strip())
        self._profile = None
        self._snapshot = None
        self.refresh()

    def _on_use_mine(self) -> None:
        mia = self._container.keys.address()
        if not mia:
            self._notice.setText(
                "No hay ninguna clave privada configurada todavía: ponla en la "
                "pestaña de Motores, en Credenciales."
            )
            return
        self._address.setText(mia)
        self._profile = None
        self.refresh()

    def _on_add_token(self) -> None:
        """Añade un token por su dirección, en la red elegida.

        Se resuelve contra el contrato —símbolo y decimales se **leen**, no se
        piden— y queda guardado en el almacén, que es lo que hace que el token
        siga en la lista la próxima vez y que aparezca también en las tarjetas de
        swap y de puentes.
        """
        red = self._chain.currentData()
        if red is None:
            self._notice.setText(
                "Elige una red concreta arriba antes de añadir un token: la "
                "dirección de un contrato sólo significa algo dentro de su red."
            )
            return
        direccion, aceptado = QInputDialog.getText(
            self,
            "Añadir token",
            f"Dirección del contrato en {CHAINS[red].name}:\n"
            "(el símbolo y los decimales se leen de la cadena)",
        )
        if not aceptado or not direccion.strip():
            return
        self._add_btn.setEnabled(False)
        self._notice.setText("Leyendo el contrato del token…")
        spawn(self._do_add_token(red, direccion.strip()))

    async def _do_add_token(self, chain_key: str, address: str) -> None:
        try:
            token = await self._container.token_lookup.by_address(chain_key, address)
        except Exception as error:
            self._notice.setText(f"No se pudo añadir el token: {error}")
            return
        finally:
            self._add_btn.setEnabled(True)
        self._container.token_store.add(token)
        if all(not known.is_same_asset(token) for known in self._tokens):
            self._tokens.append(token)
        self._notice.setText(
            f"Añadido {token.symbol} ({token.decimals} decimales) en "
            f"{CHAINS[chain_key].name}. Ya aparece en las listas de swap y de "
            f"puentes, y queda guardado para la próxima vez."
        )
        # Una relectura de la red basta para que el token salga con su saldo si
        # lo tiene: el motor de cartera lee el catálogo **más** lo guardado, así
        # que el token recién añadido entra en la siguiente pasada.
        self.refresh()

    def swap_tokens_for(self, chain_key: str) -> tuple[Token, ...]:
        """Los tokens elegibles en una red, para que la pestaña de swap los use."""
        return tokens_for_chain(self._container.token_store, chain_key)

    def set_token_in_store(self, token: Token) -> None:
        """Deja constancia de un token añadido desde otra pestaña."""
        if all(not known.is_same_asset(token) for known in self._tokens):
            self._tokens.append(token)
