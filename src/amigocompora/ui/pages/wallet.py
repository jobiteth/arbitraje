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

### La fila y el detalle: operar empieza por mirar

Cada token es una **fila entera pulsable**: el logo —redondo, con la insignia de
su red en un cuadro de esquinas redondeadas—, el nombre, y a la derecha las
cifras en su propia columna —la cantidad arriba, el valor debajo, las dos
alineadas al borde—. El nombre es el símbolo y nada más: el desempate de
homónimos y la dirección del contrato viven en el tooltip y en el detalle, donde
hay sitio para leerlos sin ensuciar la lista. Al pulsar la fila se abre su
detalle: cuánto hay, cuánto vale, el precio por unidad y lo que se ha hecho con
él («Actividad»). Antes la lista tenía una columna de botones ⇄ y una barra de
acciones que se encendía al seleccionar una fila; las dos desaparecieron. El
motivo es que había cuatro gestos para llegar a operar con un token y ninguno
para simplemente mirarlo, y la pantalla que de verdad hace falta en el medio es
la que contesta «¿qué es esto y qué le ha pasado?».

### Los mandos de la lista: la lupa, el «+» y el selector de red

La cabecera de «Tokens con saldo» lleva los tres mandos que cambian la lista: la
**lupa** despliega el buscador —y al plegarlo lo vacía, porque un filtro
escondido junto a su campo es un filtro que desaparece sin decirlo—, el **«+»**
añade un token por la dirección de su contrato, y el botón de releer vuelve a
pedir los saldos. El **selector de red** es una línea de texto —«▼ Red: Base», o
«▼ Todas las redes» cuando no hay ninguna elegida— que abre un modal con las
redes que esta cartera puede leer: el filtro es de la lista, y por eso vive en
ella y no en la cabecera de la cartera, que es donde estaba el desplegable que
había antes.

### «Actividad» es lo que ejecutó esta aplicación, y sólo eso

La lista de movimientos de un token sale del registro de ejecuciones
(`executions.jsonl`), no de la cadena: enseña lo que la aplicación emitió con él
—envíos, swaps, puentes y sus recepciones, cobros y permisos—. Una transferencia
que alguien mande a la cartera desde fuera **no aparece**, y la pantalla lo dice
con esas palabras: presentar la lista como el histórico de la cadena sería
prometer una lectura que aquí no se hace.

### Multi-cartera: el nombre, la dirección y el menú

La cabecera enseña **quién es la cartera activa y qué dirección tiene**. El
nombre —«1», «2» o el que se le haya puesto— es un botón que abre la lista para
cambiar de cartera de un clic; la dirección va debajo con el mismo botón de
copiar animado que el detalle de un token; y al lado el menú ☰ reúne todo lo que
se puede hacer con una cartera: añadir otra por su clave privada, añadir una en
**modo observación** —sólo lectura—, renombrar, mostrar su clave privada
—siempre tras la contraseña—, eliminar, bloquear o desbloquear la sesión, y
mirar una dirección sin guardarla.

### Las claves, cifradas con una contraseña

Las claves privadas que se añaden aquí se guardan **cifradas** con una contraseña
que se crea la primera vez: no hay ninguna escrita en el código ni en la
configuración, y perderla es perder esas claves —el diálogo de creación lo
dice—. La contraseña de sesión queda en memoria mientras la cartera esté
desbloqueada, porque cada firma descifra la clave con ella; «Bloquear cartera»
la olvida. La clave de la cartera heredada —la del llavero, que sigue
configurándose en Credenciales— se migra al almacén cifrado la primera vez que
se enseña, sin borrar la copia del llavero.

### La cartera que se mira y la que firma

Se pueden **leer** otras direcciones —una cartera de sólo lectura, un contrato,
la de otra persona— y por eso el perfil que se lee lleva su propia dirección y su
identidad. Pero las acciones que mueven dinero se apagan en cuanto la cartera
mirada no es la que firma, o cuando la activa no puede firmar —observación,
bloqueada, sin clave—: ofrecer el botón y fallar al pulsarlo sería enseñar a
desconfiar de los botones. La comparación es por dirección y en minúsculas,
porque el mismo texto puede venir con o sin checksum.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Final

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.execution_policy import (
    APPROVAL_KIND,
    ORDER_KIND,
    RECEIVE_KIND,
    REDEEM_KIND,
    LedgerEntry,
)
from amigocompora.app.usecases.withdraw import WALLET_ENGINE_ID
from amigocompora.domain.addresses import (
    is_evm_address,
    is_solana_address,
    shorten,
)
from amigocompora.domain.chains import CHAINS
from amigocompora.domain.errors import InvalidAmountError, WrongPasswordError
from amigocompora.domain.models import BroadcastStatus, Token
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
from amigocompora.infra.wallets import StoredWallet, WalletSource
from amigocompora.ui import icons
from amigocompora.ui.theme import (
    COLOR_CARD,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_SUCCESS,
    COLOR_TEXT,
    COLOR_WARNING,
)
from amigocompora.ui.wallet_state import WalletBalances
from amigocompora.ui.widgets import (
    Card,
    Chip,
    format_amount,
    spawn,
    token_labels,
    tokens_for_chain,
)

#: La cartera que se enseña al abrir si hay clave configurada.
READONLY_WALLET_ID: Final = "observada"
READONLY_WALLET_LABEL: Final = "Cartera observada"

#: Longitud mínima de la contraseña de las claves. No es una regla de seguridad
#: criptográfica —el keystore no la exige— sino el mínimo que evita que una tecla
#: mal pulsada se convierta en la contraseña de todo el almacén, que no tiene
#: recuperación.
MIN_PASSWORD_LENGTH: Final = 8

#: Ancho de la matriz del QR, en módulos, sin contar el margen. Un QR de dirección
#: Ethereum es de 29×29 con corrección «M»; se dibuja a 6 px por módulo.
QR_MODULE_PX: Final = 6
QR_QUIET_MODULES: Final = 2

#: Lado del logo de un token en una fila de la lista, y en su detalle.
ROW_ICON_PX: Final = 34
DETAIL_ICON_PX: Final = 52

#: Lado de la insignia de red en la esquina del logo, y grosor del marco que la
#: separa del logo: sin el marco, dos logos pegados se leen como uno solo. El
#: marco es un cuadrado de esquinas redondeadas —«semi cuadrado»— y no un anillo:
#: redondo se confundiría con el borde del logo, que ahora también lo es.
BADGE_ICON_PX: Final = 15
BADGE_FRAME_PX: Final = 2
BADGE_RADIUS_PX: Final = 4

#: Todo lo que se dibuja —insignias y glifos— se pinta al doble y se declara la
#: escala con `setDevicePixelRatio`: en una pantalla de 200 % el sistema pide el
#: pixmap a 68 px y, sin la escala, lo que sale es un logo borroso.
_ICON_SCALE: Final = 2

#: Lado del glifo del botón de copiar, en píxeles lógicos.
GLIF_PX: Final = 18

#: Decimales de la cantidad y del valor en una fila. La cantidad **recorta** —es
#: un saldo, y decir más de lo que hay es el único error que no se puede cometer—
#: y el valor redondea al céntimo —es una estimación—. Los dos, cuando redondear
#: diría cero de algo que no lo es, enseñan la cifra entera.
TOTAL_DECIMALS: Final = 4
VALUE_DECIMALS: Final = 2

#: El estado de un recibo, dicho para la pantalla. Lo que no esté en la tabla se
#: enseña tal cual: un estado nuevo de un recinto se lee mejor crudo que
#: traducido a destiempo.
_STATUS_TEXT: Final = {
    BroadcastStatus.PENDING.value: "pendiente, sin confirmar todavía",
    BroadcastStatus.REVERTED.value: "revertida: no movió nada",
    BroadcastStatus.UNKNOWN.value: "sin confirmar — no hay prueba de que ocurriera",
}


def _chain_name(chain_key: str) -> str:
    """El nombre de una red, o su clave si el catálogo no la conoce."""
    try:
        return CHAINS[chain_key].name
    except KeyError:
        return chain_key


def _format_total(value: Decimal) -> str:
    """La cantidad de un token, con cuatro decimales como máximo y sin mentir.

    **Recorta** —nunca redondea hacia arriba— porque es la cifra que se lee como
    «lo que tengo»: decir 19.4917 cuando hay 19.49166 es enseñar un saldo que la
    cadena no tiene, y el lado por el que se puede fallar sin daño es el de
    quedarse corto.

    Los ceros finales se quitan —«12.5», no «12.5000»— y una cantidad que no
    llega al cuarto decimal se enseña **entera** aunque salga larga: recortarla a
    «0.0000» diría que no hay nada, que es el único error que una cifra de saldo
    no puede cometer.
    """
    with localcontext() as contexto:
        # Holgado a propósito: `quantize` lanza si el resultado no cabe en la
        # precisión del contexto, y un token con 18 decimales y un saldo grande
        # la desborda con la del contexto por omisión.
        contexto.prec = 60
        redondeado = value.quantize(Decimal(1).scaleb(-TOTAL_DECIMALS), rounding=ROUND_DOWN)
    if redondeado == 0 and value != 0:
        return format_amount(value, full=True)
    texto = f"{redondeado:f}"
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def _format_value(value: Decimal) -> str:
    """El valor en la stablecoin de la red, con dos decimales y sin mentir.

    Se redondea al céntimo —el valor es una estimación y el céntimo más cercano
    es la lectura correcta—, salvo cuando redondear diría «0.00» de algo que no
    es cero: entonces se enseña la cifra entera, porque un cero falso es la
    única lectura peor que un número largo.
    """
    with localcontext() as contexto:
        contexto.prec = 60
        redondeado = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if redondeado == 0 and value != 0:
        return format_amount(value, full=True)
    return f"{redondeado:f}"


def _value_text(holding: TokenHolding) -> tuple[str, str]:
    """El valor de una posición y su explicación, juntos.

    Devuelve las dos cosas porque las tres ramas —a cero, sin cotización y
    valorada— tienen que decir lo mismo en la fila y en el detalle, y dos copias
    de esta decisión se desviarían: la que quedaría vieja es la que el usuario
    no está mirando.
    """
    unidad = quote_token(holding.token.chain)
    simbolo = unidad.symbol if unidad is not None else ""
    if holding.is_empty:
        # Cero no es «no se sabe»: la valoración **no pregunta** por un saldo
        # vacío —no hay nada que valorar— así que decir aquí «sin cotización»
        # contaría un fallo de precios que no ocurrió. Cero por cualquier precio
        # es cero, y eso se puede afirmar sin consultar a nadie.
        return (
            f"{_format_value(Decimal(0))} {simbolo}".strip(),
            "Esta posición está a cero, así que no hay nada que valorar y no se "
            "preguntó a ningún motor por su precio. Vale cero exactamente, no "
            "«no se sabe».",
        )
    referencia = holding.value_in_reference
    if referencia is None:
        return (
            "sin cotización",
            "Esta posición tiene fondos y no se le pudo poner precio: ningún "
            "motor activo cotiza este token contra la stablecoin de su red. No "
            "vale cero — vale «no se sabe», y por eso no entra en el total.",
        )
    return (
        f"{_format_value(referencia.as_decimal())} {referencia.symbol}",
        f"{referencia.as_decimal():f} {referencia.symbol}",
    )


def _holding_key(holding: TokenHolding) -> tuple[str, str, str]:
    """Con qué se reconoce el mismo token entre dos lecturas de la cartera."""
    return (
        holding.token.chain,
        (holding.token.address or "").lower(),
        holding.token.symbol,
    )


def _empty_layout(box: QVBoxLayout) -> None:
    """Saca y destruye todo lo que cuelga de ese layout.

    Se destruye con `deleteLater` y no se esconde: si sólo se escondieran, cada
    repintado dejaría una generación de filas vivas detrás —y una lista larga
    serían cientos de widgets existiendo para nada—.
    """
    while box.count():
        item = box.takeAt(0)
        widget = item.widget() if item is not None else None
        if widget is not None:
            widget.deleteLater()


def _entry_label(
    entry: LedgerEntry,
    *,
    bridges: frozenset[str],
    dexes: frozenset[str],
) -> str:
    """Cómo se llama, en una palabra, lo que hizo ese asiento.

    Se decide por el **tipo** del asiento, que es un dato, y para los swaps y
    los puentes —los dos que se anotan como `transaction`— por el motor que lo
    ejecutó, que también lo es: preguntarle al registro qué ranura ocupa cada
    motor es más honesto que adivinar por el texto de la etiqueta. Un motor que
    no esté en ninguna de las dos listas se anota «Movimiento» y nada más: es
    verdad —salió de aquí— aunque no diga el cómo.
    """
    if entry.kind == REDEEM_KIND:
        return "Cobro"
    if entry.kind == ORDER_KIND:
        return "Orden"
    if entry.kind == RECEIVE_KIND:
        return "Recepción"
    if entry.kind == APPROVAL_KIND:
        return "Permiso"
    if entry.engine_id == WALLET_ENGINE_ID:
        return "Envío"
    if entry.engine_id in bridges:
        return "Puente"
    if entry.engine_id in dexes:
        return "Swap"
    return "Movimiento"


def _entry_color(entry: LedgerEntry) -> str:
    """El color del chip de un asiento: verde para lo que **entra**, gris para el resto."""
    return COLOR_SUCCESS if entry.kind in (REDEEM_KIND, RECEIVE_KIND) else COLOR_MUTED


def _activity_lines(entry: LedgerEntry) -> tuple[str, ...]:
    """La segunda línea de un asiento: lo que pasó, en qué estado y con qué hash.

    El estado se calla cuando es `success`: decirlo en cada línea normal sería
    una columna de «confirmada» que nadie lee, y lo que importa es justo lo que
    no es normal —lo pendiente, lo revertido, lo que no se pudo comprobar—.

    El identificador va abreviado y sólo cuando no es de una orden: en una
    orden, el descripción ya lleva el identificador del recinto entero, porque
    no es un hash y hay que decir dónde se busca.
    """
    lineas: list[str] = []
    # Las descripciones de los recibos empiezan a veces por «—» (la parte que
    # explica la valoración va detrás del motivo); el guion suelto como primera
    # palabra se lee como un defecto.
    descripcion = entry.description.strip()
    if descripcion.startswith("—"):
        descripcion = descripcion[1:].strip()
    if descripcion:
        lineas.append(descripcion)
    if entry.status != BroadcastStatus.SUCCESS.value:
        lineas.append(f"estado: {_STATUS_TEXT.get(entry.status, entry.status)}")
    if entry.tx_hash and entry.kind != ORDER_KIND:
        lineas.append(f"hash {shorten(entry.tx_hash)}")
    return tuple(lineas)


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

    `note` es una línea extra para quien abre el diálogo desde otra pantalla y
    necesita decir algo suyo —la pestaña de predicciones avisa aquí de que a su
    wallet sólo hace falta mandarle el colateral—. El diálogo no interpreta ese
    texto: lo enseña.
    """

    def __init__(
        self,
        address: str,
        chain_key: str,
        parent: QWidget | None = None,
        *,
        note: str | None = None,
    ) -> None:
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

        if note:
            pista = QLabel(note)
            pista.setTextFormat(Qt.RichText)
            pista.setWordWrap(True)
            pista.setObjectName("hint")
            lay.addWidget(pista)

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
        QApplication.clipboard().setText(self._address.text())
        self._aviso.setText("Copiada. Se queda en el portapapeles hasta que algo la sustituya.")


class WithdrawDialog(QDialog):
    """La retirada: token, importe y destino.

    El diálogo **no firma**. Recoge lo que hace falta, lo devuelve y el caso de
    uso hace el resto, que es lo que mantiene el orden de las comprobaciones en
    un solo sitio: modo, red, destino, topes, saldo, permiso, firma. Un diálogo
    que llamara a firmar tendría su propia copia de ese orden, y la copia es
    justo lo que se desvía.

    `recipient` prefija el destino. No es una comodidad: teclear de memoria una
    dirección de 42 caracteres es la forma más común de perder fondos, y las
    pantallas que saben el destino —la pestaña de predicciones, que retira a la
    wallet de depósito— lo traen puesto. Queda editable: prefijar no es prohibir.
    """

    def __init__(
        self,
        tokens: tuple[Token, ...],
        *,
        default: Token | None = None,
        owner: str = "",
        recipient: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Retirar fondos")
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.token = QComboBox()
        for etiqueta, token in zip(token_labels(tokens), tokens, strict=True):
            self.token.addItem(icons.token_icon(token.display_symbol), etiqueta, token)
        if default is not None:
            for index in range(self.token.count()):
                if self.token.itemData(index) == default:
                    self.token.setCurrentIndex(index)
                    break
        form.addRow("Token", self.token)

        self.amount = QLineEdit()
        self.amount.setPlaceholderText("0.0")
        form.addRow("Importe", self.amount)

        self.recipient = QLineEdit(recipient)
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


class ChainPickerDialog(QDialog):
    """El modal del selector de red: una lista corta y «Todas las redes».

    Se construye con las claves que la cartera **puede leer** —no con el
    catálogo entero—, porque ofrecer una red que siempre falla es enseñar a no
    creer a la pantalla; y la primera entrada es «Todas las redes», que es el
    filtro de partida y tiene que ser alcanzable con el mismo gesto que las
    demás. Un clic elige y cierra: es una lista corta de la que se viene a sacar
    un valor y volver, y tener que seleccionar y luego acertar con un botón
    convierte un gesto en dos.
    """

    def __init__(
        self,
        chains: Sequence[str],
        current: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Red")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        self._list = QListWidget()
        # «Todas» no lleva icono: no es una red, y un logo prestado diría que lo
        # es justo en la fila que las desactiva a todas.
        todas = QListWidgetItem("Todas las redes")
        todas.setData(Qt.ItemDataRole.UserRole, None)
        self._list.addItem(todas)
        for key in chains:
            item = QListWidgetItem(icons.network_icon(key), _chain_name(key))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self._list.addItem(item)
        for fila in range(self._list.count()):
            if self._list.item(fila).data(Qt.ItemDataRole.UserRole) == current:
                self._list.setCurrentRow(fila)
                break
        else:
            # Siempre hay una fila elegida: sin selección, aceptar el modal no
            # tendría respuesta y `chosen` devolvería nada que aplicar.
            self._list.setCurrentRow(0)
        self._list.itemClicked.connect(self._on_item_clicked)
        lay.addWidget(self._list)

        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        lay.addWidget(botones)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """El clic —uno solo— elige esa fila y cierra.

        El elemento se ignora —el clic ya la dejó seleccionada antes de emitirse
        la señal—, y se nombra el parámetro para que el `connect` tenga la firma
        que espera. Los botones se quedan: el modal también se puede usar con el
        teclado, y ahí el «Aceptar» sigue siendo la forma de confirmar.
        """
        del item
        self.accept()

    def chosen(self) -> str | None:
        """La red elegida, o `None` para «Todas las redes»."""
        key = self._list.currentItem().data(Qt.ItemDataRole.UserRole)
        # `item.data` está declarado como `Any`; comprobarlo aquí es lo que
        # convierte el valor de Qt en el tipo que promete esta firma.
        return key if isinstance(key, str) else None


def badged_token_pixmap(token: Token, *, size: int = ROW_ICON_PX) -> QPixmap:
    """El logo del token —redondo— con la insignia de su red en la esquina.

    Se compone aquí porque son **dos ficheros distintos**: el mismo USDC aparece
    en tres redes y lo único que las distingue es la insignia. El token se
    recorta a un **círculo** —es lo que hace que logos de fuentes distintas se
    lean como una columna— y la insignia va sobre un cuadrado de esquinas
    redondeadas del color de la tarjeta: cuadrado del todo parecería un recorte
    mal hecho, y redondo se fundiría con el borde del token, que ya lo es.

    Si la red no tiene logo en disco no se dibuja nada en su lugar: el marco
    solo, o un icono de relleno, diría «esta red tiene marca y es ésta», que es
    justo lo que el catálogo de iconos evita. El token sí conserva su genérico,
    que no es una marca de nadie.
    """
    lado = size * _ICON_SCALE
    pixmap = QPixmap(lado, lado)
    pixmap.fill(Qt.transparent)
    insignia_icono = icons.network_icon(token.chain)
    pintor = QPainter(pixmap)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    logo = icons.token_icon(token.display_symbol)
    if not logo.isNull():
        # El recorte circular es de la pantalla, no del fichero: el mismo SVG
        # sirve redondo aquí y cuadrado donde haga falta.
        recorte = QPainterPath()
        recorte.addEllipse(QRectF(0, 0, lado, lado))
        pintor.save()
        pintor.setClipPath(recorte)
        pintor.drawPixmap(0, 0, logo.pixmap(lado, lado))
        pintor.restore()

    if not insignia_icono.isNull():
        insignia_px = BADGE_ICON_PX * _ICON_SCALE
        marco = BADGE_FRAME_PX * _ICON_SCALE
        esquina = lado - insignia_px - marco
        # El marco va por fuera de la insignia y se pinta **él solo**, relleno:
        # es una pieza pegada al logo, y el color de la tarjeta es lo que la
        # separa sin dibujarle un borde que competiría con el del icono.
        caja = QRectF(
            esquina - marco,
            esquina - marco,
            insignia_px + 2 * marco,
            insignia_px + 2 * marco,
        )
        radio = BADGE_RADIUS_PX * _ICON_SCALE
        pintor.setPen(Qt.PenStyle.NoPen)
        pintor.setBrush(QColor(COLOR_CARD))
        pintor.drawRoundedRect(caja, radio, radio)
        pintor.drawPixmap(esquina, esquina, insignia_icono.pixmap(insignia_px, insignia_px))

    pintor.end()
    pixmap.setDevicePixelRatio(_ICON_SCALE)
    return pixmap


def _copy_glyph() -> QPixmap:
    """El dibujo de «copiar»: dos hojas superpuestas, la de delante opaca.

    Se dibuja aquí y no se pide al estilo del sistema porque el glifo estándar
    cambia con el tema de Windows, y este botón tiene que leerse sobre una
    tarjeta oscura sin depender de cuál esté puesto.
    """
    lado = GLIF_PX * _ICON_SCALE
    lienzo = QPixmap(lado, lado)
    lienzo.fill(Qt.transparent)
    pintor = QPainter(lienzo)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pintor.setPen(QPen(QColor(COLOR_TEXT), 1.6 * _ICON_SCALE))
    pintor.setBrush(Qt.NoBrush)
    pintor.drawRoundedRect(
        QRectF(6 * _ICON_SCALE, 2 * _ICON_SCALE, 10 * _ICON_SCALE, 12 * _ICON_SCALE),
        2 * _ICON_SCALE,
        2 * _ICON_SCALE,
    )
    # La hoja de delante se rellena con el color de la tarjeta: es lo que hace
    # que las dos se lean como dos hojas y no como un rectángulo con rayas.
    pintor.setBrush(QColor(COLOR_CARD))
    pintor.drawRoundedRect(
        QRectF(2 * _ICON_SCALE, 6 * _ICON_SCALE, 10 * _ICON_SCALE, 12 * _ICON_SCALE),
        2 * _ICON_SCALE,
        2 * _ICON_SCALE,
    )
    pintor.end()
    lienzo.setDevicePixelRatio(_ICON_SCALE)
    return lienzo


def _check_glyph() -> QPixmap:
    """El dibujo de «copiado»: una marca verde, el acuse de recibo."""
    lado = GLIF_PX * _ICON_SCALE
    lienzo = QPixmap(lado, lado)
    lienzo.fill(Qt.transparent)
    pintor = QPainter(lienzo)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pluma = QPen(QColor(COLOR_SUCCESS), 2.4 * _ICON_SCALE)
    pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
    pluma.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pintor.setPen(pluma)
    pintor.drawPolyline(
        [
            QPointF(4 * _ICON_SCALE, 9.5 * _ICON_SCALE),
            QPointF(7.5 * _ICON_SCALE, 13 * _ICON_SCALE),
            QPointF(14 * _ICON_SCALE, 5.5 * _ICON_SCALE),
        ]
    )
    pintor.end()
    lienzo.setDevicePixelRatio(_ICON_SCALE)
    return lienzo


def _search_glyph() -> QPixmap:
    """La lupa del buscador: el aro y el mango, en el color del texto.

    Se dibuja aquí por la misma razón que el glifo de copiar: el icono estándar
    del sistema cambia con el tema de Windows, y este mando tiene que leerse
    sobre la tarjeta oscura sin depender de cuál esté puesto.
    """
    lado = GLIF_PX * _ICON_SCALE
    lienzo = QPixmap(lado, lado)
    lienzo.fill(Qt.transparent)
    pintor = QPainter(lienzo)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pluma = QPen(QColor(COLOR_TEXT), 1.8 * _ICON_SCALE)
    pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
    pintor.setPen(pluma)
    pintor.setBrush(Qt.BrushStyle.NoBrush)
    pintor.drawEllipse(
        QPointF(7.5 * _ICON_SCALE, 7.5 * _ICON_SCALE),
        4.5 * _ICON_SCALE,
        4.5 * _ICON_SCALE,
    )
    pintor.drawLine(
        QPointF(11 * _ICON_SCALE, 11 * _ICON_SCALE),
        QPointF(15.2 * _ICON_SCALE, 15.2 * _ICON_SCALE),
    )
    pintor.end()
    lienzo.setDevicePixelRatio(_ICON_SCALE)
    return lienzo


def _plus_glyph() -> QPixmap:
    """El signo «+» del botón de añadir token, en el color del texto."""
    lado = GLIF_PX * _ICON_SCALE
    lienzo = QPixmap(lado, lado)
    lienzo.fill(Qt.transparent)
    pintor = QPainter(lienzo)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pluma = QPen(QColor(COLOR_TEXT), 2 * _ICON_SCALE)
    pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
    pintor.setPen(pluma)
    pintor.drawLine(
        QPointF(9 * _ICON_SCALE, 3.5 * _ICON_SCALE),
        QPointF(9 * _ICON_SCALE, 14.5 * _ICON_SCALE),
    )
    pintor.drawLine(
        QPointF(3.5 * _ICON_SCALE, 9 * _ICON_SCALE),
        QPointF(14.5 * _ICON_SCALE, 9 * _ICON_SCALE),
    )
    pintor.end()
    lienzo.setDevicePixelRatio(_ICON_SCALE)
    return lienzo


def _menu_glyph() -> QPixmap:
    """El signo «☰» del menú de carteras, en el color del texto.

    Se dibuja aquí por el mismo motivo que los demás glifos: el icono estándar
    del sistema cambia con el tema de Windows y este botón tiene que leerse sobre
    la tarjeta oscura sin depender de cuál esté puesto.
    """
    lado = GLIF_PX * _ICON_SCALE
    lienzo = QPixmap(lado, lado)
    lienzo.fill(Qt.transparent)
    pintor = QPainter(lienzo)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pluma = QPen(QColor(COLOR_TEXT), 1.8 * _ICON_SCALE)
    pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
    pintor.setPen(pluma)
    for alto in (4.5, 9.0, 13.5):
        pintor.drawLine(
            QPointF(3.5 * _ICON_SCALE, alto * _ICON_SCALE),
            QPointF(14.5 * _ICON_SCALE, alto * _ICON_SCALE),
        )
    pintor.end()
    lienzo.setDevicePixelRatio(_ICON_SCALE)
    return lienzo


class _CopyButton(QToolButton):
    """El icono que copia una dirección, con una marca que confirma que se copió.

    Copiar no cambia nada en la pantalla, así que sin una confirmación propia el
    usuario sólo se entera de que funcionó pegando en otro sitio —y si no pegó,
    ya no sabe si falló el botón o el pegado—. Al pulsarlo, el glifo se funde a
    una marca verde durante un segundo largo y vuelve solo: la animación **es**
    la confirmación.

    Copia **lo que se le dio con `set_address`**, que es lo que se está viendo en
    el sitio desde el que se usa —el contrato de un token, la dirección de la
    cartera—: quien decide qué copia es quien construye la pantalla, y así el
    botón no puede copiar una dirección distinta de la que está en pantalla.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._address = ""
        self._blend = 0.0
        self._copy_pixmap = _copy_glyph()
        self._check_pixmap = _check_glyph()
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(140)
        self._animation.valueChanged.connect(self._on_animation_value)
        # La marca se queda un segundo largo y se va sola: es un acuse de
        # recibo, no un estado del botón.
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(1000)
        self._hold.timeout.connect(self._fade_back)
        self.clicked.connect(self._on_clicked)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(GLIF_PX, GLIF_PX))
        self.setFixedSize(30, 30)
        self._paint_icon()

    def set_address(self, address: str, *, what: str = "la dirección del contrato") -> None:
        self._address = address
        self.setToolTip(
            f"Copia {what} {address}. Se queda en el portapapeles hasta que algo "
            f"la sustituya."
        )

    def _on_clicked(self) -> None:
        if not self._address:
            return
        QApplication.clipboard().setText(self._address)
        self._hold.stop()
        self._animate_to(1.0)
        self._hold.start()

    def _fade_back(self) -> None:
        self._animate_to(0.0)

    def _animate_to(self, destino: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._blend)
        self._animation.setEndValue(destino)
        self._animation.start()

    def _on_animation_value(self, value: float) -> None:
        self._blend = float(value)
        self._paint_icon()

    def _paint_icon(self) -> None:
        """El glifo de ahora: la copia y la marca fundidas según `_blend`.

        Se compone en cada fotograma en vez de guardar dos iconos y cambiar de
        uno a otro porque el cruce corto es lo que hace que la marca se lea como
        una respuesta al clic y no como otro botón distinto.
        """
        lienzo = QPixmap(self._copy_pixmap.size())
        lienzo.setDevicePixelRatio(_ICON_SCALE)
        lienzo.fill(Qt.transparent)
        pintor = QPainter(lienzo)
        pintor.setOpacity(1.0 - self._blend)
        pintor.drawPixmap(0, 0, self._copy_pixmap)
        pintor.setOpacity(self._blend)
        pintor.drawPixmap(0, 0, self._check_pixmap)
        pintor.end()
        self.setIcon(QIcon(lienzo))


class _TokenRow(QFrame):
    """Una fila de la lista: el logo con su red, el nombre y las cifras al lado.

    La fila **entera** es el botón que abre el detalle del token. Antes cada
    fila tenía su propio ⇄ en la última columna y la selección encendía una
    barra de acciones debajo; las dos cosas desaparecieron porque operar con un
    token empieza por mirarlo, y para llegar al ⇄ había que seleccionar la fila,
    bajar a la barra y **cambiar de pestaña**: cuatro gestos para algo que ahora
    es uno, y ninguno para lo que de verdad se venía a hacer, que era mirar.

    El nombre es el símbolo y nada más —el desempate de homónimos y la dirección
    viven en el tooltip— y las cifras van en su propia columna a la derecha,
    alineadas al borde: así los números de la lista se comparan de un salto
    vertical, que es lo que una lista de saldos viene a hacer.
    """

    clicked = Signal(object)

    def __init__(self, holding: TokenHolding, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.holding = holding
        token = holding.token
        red = _chain_name(token.chain)
        self.setObjectName("tokenRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        logo = QLabel()
        logo.setPixmap(badged_token_pixmap(token))
        logo.setFixedSize(ROW_ICON_PX, ROW_ICON_PX)
        lay.addWidget(logo)

        self._name = QLabel(token.display_symbol)
        self._name.setStyleSheet("font-weight: 600;")
        self._name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._name, 1)

        # Las cifras, en columna propia: la cantidad arriba y el valor debajo,
        # las dos contra el borde derecho.
        cifras = QVBoxLayout()
        cifras.setSpacing(1)
        self._amount = QLabel(f"{_format_total(holding.as_decimal())} {token.symbol}")
        self._amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # La cifra **entera** en el tooltip: el recorte es de la pantalla, y el
        # dato completo ya está calculado — esconderlo tras un botón sería
        # esconder lo que no cuesta nada enseñar.
        self._amount.setToolTip(
            f"{holding.as_decimal():f} {token.symbol} en {red}, {token.decimals} "
            f"decimales" + ("" if token.address else " (moneda nativa de la red)")
        )
        cifras.addWidget(self._amount)
        texto_valor, ayuda_valor = _value_text(holding)
        self._value = QLabel(texto_valor)
        self._value.setObjectName("hint")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._value.setToolTip(ayuda_valor)
        cifras.addWidget(self._value)
        lay.addLayout(cifras)

        self.setToolTip(
            f"{token.qualified_symbol} en {red}: {holding.as_decimal():f} "
            f"{token.symbol}, {texto_valor.lower()}. Pulsa para ver el token y "
            f"lo que se ha hecho con él."
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # Se abre al **soltar** y sólo si el botón se suelta dentro de la fila:
        # abrir al pulsar haría que un arrastre que empieza aquí acabara
        # navegando, que es la mitad de los arrastres.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self.holding)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # El teclado abre igual que el ratón: una fila que se enfoca con el
        # tabulador y no responde a la tecla es una fila rota para quien navega
        # sin ratón. Enter y espacio, que son las dos que «pulsan» en Windows.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.holding)
            return
        super().keyPressEvent(event)


class WalletPickerDialog(QDialog):
    """La lista de carteras del libro: un clic elige y cierra.

    De cada cartera se enseña su etiqueta **y** su dirección en corto: con varias
    carteras las etiquetas pueden parecerse —o repetirse, porque la identidad es
    la dirección— y elegir «2» sin ver a dónde apunta es elegir a ciegas.

    Un clic elige, y no hay que acertar después con un botón «Elegir»: es una
    lista corta de la que se viene a sacar un valor y volver, y cada gesto de más
    es un gesto en el que se puede elegir la cartera equivocada.
    """

    def __init__(
        self,
        wallets: Sequence[StoredWallet],
        current_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Carteras")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        self._list = QListWidget()
        for wallet in wallets:
            item = QListWidgetItem(f"{wallet.label} — {shorten(wallet.address)}")
            item.setData(Qt.ItemDataRole.UserRole, wallet.wallet_id)
            item.setToolTip(
                f"{wallet.label} · {wallet.address} · clave en {wallet.source.label}"
            )
            self._list.addItem(item)
        for fila in range(self._list.count()):
            if self._list.item(fila).data(Qt.ItemDataRole.UserRole) == current_id:
                self._list.setCurrentRow(fila)
                break
        else:
            # Siempre hay una fila elegida: quien abre el diálogo comprueba antes
            # que hay carteras, y aceptar sin selección no tendría respuesta.
            self._list.setCurrentRow(0)
        self._list.itemClicked.connect(self._on_item_clicked)
        lay.addWidget(self._list)

        botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        lay.addWidget(botones)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """Un clic basta: ya está seleccionada y se acepta con esa.

        El elemento se ignora —el clic ya la dejó seleccionada antes de emitir la
        señal—, y se nombra el parámetro para que el `connect` tenga la firma que
        espera.
        """
        del item
        self.accept()

    def chosen(self) -> str | None:
        """El identificador de la cartera elegida, o `None` si no hay ninguna.

        Siempre hay una fila seleccionada —quien abre el diálogo comprueba antes
        que hay carteras, y la primera queda elegida—, así que no se pregunta si
        hay item; lo que sí se comprueba es el dato, porque `data` devuelve `Any`
        y el `isinstance` es lo que lo convierte en el tipo que promete la firma.
        """
        value = self._list.currentItem().data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else None


class PasswordDialog(QDialog):
    """La contraseña de las claves: crearla la primera vez, pedirla después.

    En modo crear pide la contraseña **dos veces** —una tecleada una sola vez y
    mal se convierte en claves que nadie puede abrir, y eso no tiene arreglo— y
    avisa de que no hay forma de recuperarla. En modo pedir pide una y admite un
    `error` que se enseña dentro: la comprobación la hace quien llama —descifrar
    de verdad— y lo que falló se dice aquí sin cerrar el diálogo, para no perder
    la lista de lo que se estaba haciendo.

    El texto que se teclea no sale de este diálogo en ningún mensaje: `password`
    es lo único que devuelve, y quien lo recibe lo pasa al proveedor.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        create: bool,
        motivo: str = "",
        error: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._create = create
        self.setWindowTitle("Crear la contraseña" if create else "Contraseña de las claves")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setMinimumWidth(320)
        form.addRow("Contraseña nueva" if create else "Contraseña", self._password)
        self._repeat: QLineEdit | None = None
        if create:
            self._repeat = QLineEdit()
            self._repeat.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Repite la contraseña", self._repeat)
        lay.addLayout(form)

        pista = QLabel(
            motivo
            or (
                "Protege las claves privadas guardadas en este equipo. No se guarda "
                "en ningún sitio del que se pueda recuperar: si se pierde, esas "
                "claves no se pueden abrir."
                if create
                else "Abre el almacén cifrado de carteras."
            )
        )
        pista.setWordWrap(True)
        pista.setObjectName("hint")
        lay.addWidget(pista)

        self._error = QLabel(error or "")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {COLOR_WARNING};")
        self._error.setVisible(bool(error))
        lay.addWidget(self._error)

        caja = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        caja.accepted.connect(self._on_accept)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def _on_accept(self) -> None:
        """Valida lo mínimo y sólo entonces cierra. El error se enseña dentro."""
        password = self._password.text()
        if not password:
            self._show_error("La contraseña no puede estar vacía.")
            return
        if self._create:
            if len(password) < MIN_PASSWORD_LENGTH:
                self._show_error(
                    f"Al menos {MIN_PASSWORD_LENGTH} caracteres: esta contraseña "
                    f"protege todas las claves cifradas y no se puede recuperar."
                )
                return
            if self._repeat is not None and password != self._repeat.text():
                self._show_error("Las dos no coinciden. Vuelve a escribirla igual en los dos campos.")
                return
        self.accept()

    def _show_error(self, texto: str) -> None:
        self._error.setText(texto)
        self._error.setVisible(True)

    def password(self) -> str:
        return self._password.text()


class SigningWalletDialog(QDialog):
    """Añadir una cartera con clave privada: su nombre y la clave.

    El nombre viene propuesto con el primer número libre —«1», «2»— porque una
    cartera sin nombre es una dirección más, y elegir entre direcciones es
    justamente lo que estas carteras existen para no hacer. Queda editable: la
    etiqueta la valida el proveedor, que es el mismo sitio que la guardará.

    La clave se escribe con eco de contraseña y **no se valida aquí**: quien
    deriva la dirección y rechaza lo que no sea una clave es `add_signing`, y su
    mensaje no cita lo que se escribió. Este diálogo no la escribe en ningún
    registro ni la mete en ningún texto.
    """

    def __init__(self, default_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Añadir cartera")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        form = QFormLayout()
        self.label = QLineEdit(default_label)
        form.addRow("Nombre", self.label)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("0x…")
        self.key.setMinimumWidth(420)
        form.addRow("Clave privada", self.key)
        lay.addLayout(form)

        pista = QLabel(
            "La clave se guarda <b>cifrada</b> con la contraseña de las carteras y "
            "se descifra sólo en el momento de firmar. No se envía a ningún sitio."
        )
        pista.setTextFormat(Qt.RichText)
        pista.setWordWrap(True)
        pista.setObjectName("hint")
        lay.addWidget(pista)

        caja = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        caja.accepted.connect(self.accept)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def chosen(self) -> tuple[str, str]:
        """La etiqueta y la clave tal como se escribieron."""
        return self.label.text().strip(), self.key.text().strip()


class WatchWalletDialog(QDialog):
    """Añadir una cartera en modo observación: su nombre y su dirección.

    Se admiten las dos familias —EVM y Solana— porque mirar no firma: la familia
    la deduce la forma de la dirección y el diálogo no la pregunta. La dirección
    no se valida aquí: el proveedor la valida con las mismas reglas que usa al
    leer, y su mensaje dice qué forma se esperaba.
    """

    def __init__(self, default_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Añadir cartera en modo observación")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        form = QFormLayout()
        self.label = QLineEdit(default_label)
        form.addRow("Nombre", self.label)
        self.address = QLineEdit()
        self.address.setPlaceholderText("0x… o dirección de Solana")
        self.address.setMinimumWidth(420)
        form.addRow("Dirección", self.address)
        lay.addLayout(form)

        pista = QLabel(
            "Se leerán sus saldos y se podrá depositar en ella, pero <b>no se puede "
            "firmar</b>: no hay ninguna clave guardada. Se puede ascender a cartera "
            "con clave añadiéndola después con su clave privada."
        )
        pista.setTextFormat(Qt.RichText)
        pista.setWordWrap(True)
        pista.setObjectName("hint")
        lay.addWidget(pista)

        caja = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        caja.accepted.connect(self.accept)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def chosen(self) -> tuple[str, str]:
        """La etiqueta y la dirección tal como se escribieron."""
        return self.label.text().strip(), self.address.text().strip()


class RevealKeyDialog(QDialog):
    """La clave privada en pantalla, con su aviso y su botón de copiar.

    Es el único sitio de la aplicación donde una clave privada se enseña, y llega
    aquí ya descifrada: el diálogo no la descifra ni la valida —eso ya está
    hecho— y no la guarda más allá de lo que dura el campo de texto. Sólo se
    cierra; no hay nada que aceptar.
    """

    def __init__(self, label: str, private_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Clave privada de «{label}»")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        aviso = QLabel(
            "Quien tenga esta clave puede mover todo lo que hay en esta cartera. "
            "No la pegues en un chat, en una web ni en un formulario que no sea "
            "para restaurar esta misma cartera en otra aplicación."
        )
        aviso.setWordWrap(True)
        aviso.setStyleSheet(f"color: {COLOR_WARNING};")
        lay.addWidget(aviso)

        fila = QHBoxLayout()
        fila.setSpacing(4)
        self._key = QLineEdit(private_key)
        self._key.setReadOnly(True)
        self._key.setCursorPosition(0)
        self._key.setStyleSheet("font-family: Consolas, monospace;")
        self._key.setMinimumWidth(420)
        self._copy = _CopyButton()
        self._copy.set_address(private_key, what="la clave privada")
        fila.addWidget(self._key, 1)
        fila.addWidget(self._copy, 0)
        lay.addLayout(fila)

        pista = QLabel(
            "Está guardada cifrada con la contraseña de las carteras; esto es sólo "
            "su copia para restaurarla o respaldarla."
        )
        pista.setObjectName("hint")
        pista.setWordWrap(True)
        lay.addWidget(pista)

        caja = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)


class DeleteWalletDialog(QDialog):
    """Confirmar el borrado, diciendo qué se borra y qué no.

    El texto cambia con la fuente de la clave porque lo que se pierde no es lo
    mismo: una cartera cifrada se lleva su clave —y no hay copia en ningún otro
    sitio—, la del llavero deja intacta la copia del llavero, y una en
    observación no guarda ninguna clave. Un aviso único para los tres casos diría
    de más en unos y de menos en otros.
    """

    def __init__(self, wallet: StoredWallet, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._confirmed = False
        self.setWindowTitle("Eliminar cartera")
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        pregunta = QLabel(
            f"Se va a eliminar la cartera «{wallet.label}» ({wallet.address}) del "
            f"libro de carteras."
        )
        pregunta.setWordWrap(True)
        lay.addWidget(pregunta)

        if wallet.source is WalletSource.KEYSTORE:
            detalle = (
                "Su clave cifrada se borra con ella y no hay copia en ningún otro "
                "sitio: sin esta entrada no se puede recuperar."
            )
        elif wallet.source is WalletSource.KEYRING:
            detalle = (
                "La copia del llavero no se toca: la clave sigue en Credenciales y "
                "se podrá añadir otra vez. Lo que se borra es la entrada del libro, "
                "y el llavero no volverá a aparecer solo."
            )
        else:
            detalle = (
                "No guarda ninguna clave: al eliminarla sólo se deja de leer esa "
                "dirección, y se puede volver a añadir cuando quieras."
            )
        aviso = QLabel(detalle)
        aviso.setWordWrap(True)
        aviso.setStyleSheet(f"color: {COLOR_WARNING};")
        lay.addWidget(aviso)

        caja = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        eliminar = caja.addButton("Eliminar", QDialogButtonBox.ButtonRole.DestructiveRole)
        eliminar.setStyleSheet(f"color: {COLOR_DANGER};")
        eliminar.clicked.connect(self._on_delete)
        caja.rejected.connect(self.reject)
        lay.addWidget(caja)

    def _on_delete(self) -> None:
        self._confirmed = True
        self.accept()

    def confirmed(self) -> bool:
        """Si se cerró confirmando el borrado y no cancelando."""
        return self._confirmed


class WalletPage(QWidget):
    """Cartera: saldos por red, con QR para depositar y formulario para retirar."""

    #: Pide a la pestaña de swap que prepare un par con este token. La señal lleva
    #: el token y la red porque el swap tiene su propio desplegable de red y hay
    #: que ponerlo en la misma antes de tocar las patas.
    swap_requested = Signal(object)

    #: La cartera activa cambió —se eligió otra, se añadió, se renombró o se
    #: borró—. Las páginas que firman leen la dirección del proveedor compartido,
    #: así que sin un aviso seguirían pintando los gates y los saldos de la
    #: cartera anterior con la misma seguridad que si fueran los nuevos. La
    #: ventana la conecta al refresco de swap y de predicción, igual que ya
    #: conecta `credentials_changed`.
    wallet_changed = Signal()

    def __init__(
        self,
        container: Container,
        parent: QWidget | None = None,
        *,
        balances: WalletBalances | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._snapshot: WalletSnapshot | None = None
        self._tokens: list[Token] = list(container.token_store.load())
        self._profile: WalletProfile | None = None
        #: Los saldos compartidos con la tarjeta de swap. Esta página los **lee**
        #: enteros —nueve redes, porque necesita el patrimonio— y los **publica**
        #: en la caché común, que es lo que evita que la tarjeta de al lado vuelva
        #: a preguntar por la misma red que ésta acaba de leer. Si nadie la pasa
        #: —una `WalletPage` suelta, que es como la montan las pruebas— se crea una
        #: propia: funciona igual y sólo deja de compartir.
        self._balances = balances or WalletBalances(container.read_wallet)
        #: Las filas de la última lectura, **con** las que están a cero. Se
        #: guardan enteras y se filtran al pintar: si el filtro se aplicara aquí,
        #: desmarcar «Sólo con saldo» no tendría nada que volver a enseñar y el
        #: botón parecería roto.
        self._rows: list[tuple[ChainHoldings, TokenHolding]] = []
        #: Las filas **pintadas** ahora mismo. Es lo que leen las pruebas y lo
        #: que se recorre al reabrir el detalle; la lista se reconstruye entera en
        #: cada repintado, así que esta lista y lo que se ve no pueden divergir.
        self._row_widgets: list[_TokenRow] = []
        #: El token cuyo detalle está abierto, o `None` si se ve la lista. Se
        #: guarda el holding por su **clave** (`_holding_key`) y no la fila: la
        #: fila puede desaparecer en la siguiente lectura, y el detalle tiene que
        #: re-colarse de la lectura vigente o volver a la lista.
        self._detail_holding: TokenHolding | None = None
        #: Número de la lectura en curso. Ver `refresh`: cada lectura se lleva el
        #: suyo y lo que llega tarde se reconoce por no ser el último.
        self._read_id = 0
        #: El filtro de red de la lista: `None` es «Todas las redes». Es estado
        #: de la vista —no de la lectura—, así que elegir red no vuelve a pedir
        #: un nodo: sólo se repintan las filas que ya están.
        self._chain_key: str | None = None
        #: Las redes que esta cartera puede leer, que son las que ofrece el
        #: modal del selector. Se rellenan con cada perfil leído.
        self._chain_keys: tuple[str, ...] = ()

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

        self._search.textChanged.connect(self._repaint_rows)
        self._only_positive.toggled.connect(self._repaint_rows)
        # La cartera con la que se abre es la activa del libro —o la heredada del
        # llavero, que es la activa mientras no se toque el libro—. Sin esto la
        # pestaña arranca en blanco teniendo cartera: el usuario tendría que
        # cambiar de cartera para ver unos saldos que la aplicación ya sabe de
        # quién son.
        self._sync_wallet_header()
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

        # Fila 1: quién es la cartera —su nombre, que abre la lista para
        # cambiar— y el menú con todo lo que se puede hacer con ella. El nombre
        # va primero porque con varias carteras saber cuál manda es la mitad de
        # operar con la correcta, y es lo que hay que pulsar para cambiar.
        self._wallet_btn = QPushButton("Sin cartera ▾")
        self._wallet_btn.setObjectName("link")
        self._wallet_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wallet_btn.clicked.connect(self._on_pick_wallet)

        self._menu_btn = QToolButton()
        self._menu_btn.setObjectName("iconAction")
        self._menu_btn.setIcon(QIcon(_menu_glyph()))
        self._menu_btn.setIconSize(QSize(GLIF_PX, GLIF_PX))
        # Despliega al primer clic: con `MenuButtonPopup` haría falta pulsar el
        # triangulito de al lado, que en un botón sin texto es todo el botón.
        self._menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu_btn.setToolTip(
            "Carteras: añadir otra, añadir en modo observación, renombrar, mostrar "
            "la clave privada, eliminar, bloquear o desbloquear…"
        )
        self._menu_btn.setAccessibleName("Menú de carteras")
        self._menu = QMenu(self._menu_btn)
        # Los tooltips de las acciones son lo que explica por qué una está
        # apagada —una cartera en observación no tiene clave que enseñar—.
        self._menu.setToolTipsVisible(True)
        self._menu.aboutToShow.connect(self._paint_menu)
        self._menu_btn.setMenu(self._menu)

        self._act_add_signing = self._menu.addAction("Añadir cartera (clave privada)…")
        self._act_add_signing.triggered.connect(self._on_add_signing_wallet)
        self._act_add_watch = self._menu.addAction("Añadir cartera en modo observación…")
        self._act_add_watch.triggered.connect(self._on_add_watch_wallet)
        self._menu.addSeparator()
        self._act_rename = self._menu.addAction("Renombrar cartera…")
        self._act_rename.triggered.connect(self._on_rename_wallet)
        self._act_reveal = self._menu.addAction("Mostrar clave privada…")
        self._act_reveal.triggered.connect(self._on_reveal_key)
        self._act_delete = self._menu.addAction("Eliminar cartera…")
        self._act_delete.triggered.connect(self._on_delete_wallet)
        self._menu.addSeparator()
        self._act_lock = self._menu.addAction("Bloquear cartera")
        self._act_lock.triggered.connect(self._on_toggle_lock)
        self._act_peek = self._menu.addAction("Mirar una dirección (sin guardarla)…")
        self._act_peek.setToolTip(
            "Lee los saldos de una dirección que no es la cartera activa —una "
            "cartera de sólo lectura, un contrato, la de otra persona— sin "
            "guardarla en el libro. Para volver a la activa, pulsa su nombre."
        )
        self._act_peek.triggered.connect(self._on_lookup_address)

        cabecera = QHBoxLayout()
        cabecera.setSpacing(6)
        cabecera.addWidget(self._wallet_btn)
        cabecera.addStretch(1)
        cabecera.addWidget(self._menu_btn)
        card.add_row(cabecera)

        # Fila 2: la dirección, a todo el ancho del panel y con el mismo botón de
        # copiar animado que el detalle de un token. Antes era un campo de sólo
        # lectura con su etiqueta «DIRECCIÓN» encima y dos botones debajo, y la
        # etiqueta gastaba una línea para decir lo que el contenido ya dice.
        direccion = QHBoxLayout()
        direccion.setSpacing(4)
        self._address = QLabel("")
        self._address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._address.setStyleSheet("font-family: Consolas, monospace;")
        self._address.setWordWrap(True)
        self._copy_btn = _CopyButton()
        self._copy_btn.setEnabled(False)
        direccion.addWidget(self._address, 1)
        direccion.addWidget(self._copy_btn, 0, Qt.AlignmentFlag.AlignTop)
        card.add_row(direccion)

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

        # Depositar y Retirar en la misma fila: son las dos que mueven dinero y se leen
        # juntas. Con el panel de ~340 px caben, cada una con su texto completo.
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self._deposit_btn, 1)
        actions.addWidget(self._withdraw_btn, 1)
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
    # El libro de carteras: la activa, el menú y sus diálogos
    # ------------------------------------------------------------------ #
    def _sync_wallet_header(self) -> None:
        """La cabecera con la cartera de ahora: su nombre, su dirección y su copia."""
        keys = self._container.keys
        active = keys.active()
        if active is None:
            self._wallet_btn.setText("Sin cartera ▾")
            self._wallet_btn.setToolTip(
                "No hay ninguna cartera en el libro. Añade una con el menú ☰ —con "
                "su clave privada o en modo observación—."
            )
        else:
            self._wallet_btn.setText(f"{active.label} ▾")
            self._wallet_btn.setToolTip(
                f"Cartera activa: «{active.label}» ({shorten(active.address)}), con "
                f"clave en {active.source.label}. Pulsa para cambiar de cartera."
            )
        self._set_address_text(keys.address() or "")

    def _set_address_text(self, address: str) -> None:
        """La dirección de la cabecera, con su botón de copiar y su explicación.

        Las tres cosas se ponen en un solo sitio porque son la misma: lo que se
        copia tiene que ser lo que se está viendo, y durante una mirada sin
        guardar lo que se ve no es la cartera activa. Con el botón atado a la
        activa, copiaría una dirección distinta de la de la pantalla.
        """
        self._address.setText(address)
        self._copy_btn.setEnabled(bool(address))
        if not address:
            self._address.setToolTip("")
            return
        self._copy_btn.set_address(address, what="la dirección de la cartera")
        keys = self._container.keys
        active = keys.active()
        activa = (keys.address() or "").lower()
        if active is not None and activa == address.lower():
            self._address.setToolTip(
                f"La dirección de la cartera activa «{active.label}» "
                f"({active.source.label}). Desde esta dirección es desde donde "
                f"la aplicación firma."
            )
        else:
            self._address.setToolTip(
                "La dirección que se está mirando, que no es la cartera activa. Se "
                "leen sus saldos; las acciones que mueven dinero están apagadas. "
                "Para volver a la activa, pulsa su nombre arriba."
            )

    def _wallet_book_changed(self) -> None:
        """El libro cambió: cabecera, lectura y aviso al resto de la aplicación.

        Se rehace la lectura entera y no sólo la cabecera: la lista y el total
        son de la cartera anterior, y dejarlos bajo la dirección nueva sería
        enseñar un saldo que ya no es el de nadie. La señal hace lo que ya hace
        `credentials_changed` en la ventana —avisar de que el dueño que firma
        cambió— para las páginas que no viven aquí.
        """
        self._profile = None
        self._sync_wallet_header()
        self.refresh()
        self.wallet_changed.emit()

    def _paint_menu(self) -> None:
        """Pone el menú en el estado de ahora, justo antes de desplegarse.

        Se repinta al abrir y no en cada cambio porque un menú puede quedarse
        obsoleto por cosas que pasan fuera de esta página —se desbloqueó la
        sesión en un diálogo, se migró una clave— y el momento en el que no
        puede mentir es el momento en el que alguien lo va a usar.

        Una acción apagada lleva su motivo en el tooltip: un menú con opciones
        grises y sin explicación es un menú que empuja a buscar cómo saltárselo.
        """
        keys = self._container.keys
        active = keys.active()
        self._act_rename.setEnabled(active is not None)
        self._act_delete.setEnabled(active is not None)
        self._act_reveal.setEnabled(
            active is not None and active.source is not WalletSource.WATCH
        )
        if active is None:
            motivo = "No hay ninguna cartera todavía: añádela con las dos primeras opciones."
            self._act_rename.setToolTip(motivo)
            self._act_reveal.setToolTip(motivo)
            self._act_delete.setToolTip(motivo)
        elif active.source is WalletSource.WATCH:
            self._act_reveal.setToolTip(
                f"«{active.label}» está en modo observación: no guarda ninguna "
                f"clave privada que enseñar."
            )
        else:
            self._act_reveal.setToolTip(
                "Enseña la clave privada de la cartera activa. Pide la contraseña "
                "aunque la sesión esté desbloqueada."
            )
        if keys.has_password():
            self._act_lock.setVisible(True)
            desbloqueada = keys.is_unlocked()
            self._act_lock.setText(
                "Bloquear cartera" if desbloqueada else "Desbloquear cartera…"
            )
            self._act_lock.setToolTip(
                "Olvida la contraseña de esta sesión: para volver a firmar habrá "
                "que escribirla otra vez."
                if desbloqueada
                else "Pide la contraseña de las claves para poder firmar sin "
                "volver a escribirla. Se olvida al cerrar la aplicación."
            )
        else:
            # Sin ninguna clave cifrada no hay sesión que bloquear: una opción
            # que no puede hacer nada no se enseña.
            self._act_lock.setVisible(False)

    def _on_pick_wallet(self) -> None:
        """El nombre de la cartera abre la lista; un clic elige y vuelve a leer."""
        keys = self._container.keys
        wallets = keys.wallets()
        if not wallets:
            self._notice.setText(
                "No hay ninguna cartera todavía: añádela con el menú ☰ —con su "
                "clave privada para firmar, o en modo observación para sólo leerla."
            )
            return
        active = keys.active()
        dialogo = WalletPickerDialog(
            wallets, active.wallet_id if active is not None else None, parent=self
        )
        if dialogo.exec() != QDialog.Accepted:
            return
        elegida = dialogo.chosen()
        if elegida is None:
            return
        self._set_active_wallet(elegida)

    def _set_active_wallet(self, wallet_id: str) -> None:
        """Cambia la cartera activa, aquí y en todo lo que firma.

        Se aplica aunque sea la que ya estaba elegida: volver a elegirla es la
        forma de regresar a la activa después de mirar otra dirección, y eso
        también cambia lo que se está leyendo.
        """
        try:
            self._container.keys.set_active(wallet_id)
        except Exception as error:
            self._notice.setText(f"No se pudo cambiar de cartera: {error}")
            return
        self._wallet_book_changed()
        activa = self._container.keys.active()
        if activa is not None:
            self._notice.setText(
                f"Cartera activa: «{activa.label}» ({shorten(activa.address)}). "
                f"Todo lo que firma —swap, predicción, retiros— sale de esta "
                f"dirección."
            )

    def _on_add_signing_wallet(self) -> None:
        """Añade una cartera con clave privada, cifrándola con la contraseña.

        La contraseña se pide **antes** de cifrar nada: la primera vez se crea
        —dos veces, con el aviso de que no se recupera— y a partir de ahí se
        pide la misma, porque todas las claves comparten un único almacén. La
        clave la valida el proveedor, que deriva su dirección y rechaza lo que no
        sea una clave sin citarla en el mensaje.
        """
        keys = self._container.keys
        dialogo = SigningWalletDialog(keys.next_label(), parent=self)
        if dialogo.exec() != QDialog.Accepted:
            return
        label, private_key = dialogo.chosen()
        if not private_key:
            self._notice.setText("No se añadió nada: falta la clave privada.")
            return
        if not label:
            self._notice.setText("No se añadió nada: ponele un nombre a la cartera.")
            return
        password = (
            self._ask_existing_password("Añadir una cartera cifrada")
            if keys.has_password()
            else self._ask_new_password()
        )
        if password is None:
            return
        try:
            wallet = keys.add_signing(private_key, label, password)
        except Exception as error:
            self._notice.setText(f"No se añadió la cartera: {error}")
            return
        self._wallet_book_changed()
        self._notice.setText(
            f"Añadida la cartera «{wallet.label}» ({shorten(wallet.address)}). Su "
            f"clave está cifrada y ya es la cartera activa."
        )

    def _on_add_watch_wallet(self) -> None:
        """Añade una cartera de sólo lectura: se leen sus saldos y no firma."""
        keys = self._container.keys
        dialogo = WatchWalletDialog(keys.next_label(), parent=self)
        if dialogo.exec() != QDialog.Accepted:
            return
        label, address = dialogo.chosen()
        if not address:
            self._notice.setText("No se añadió nada: falta la dirección.")
            return
        if not label:
            self._notice.setText("No se añadió nada: ponele un nombre a la cartera.")
            return
        try:
            wallet = keys.add_watch(address, label)
        except Exception as error:
            self._notice.setText(f"No se añadió la cartera: {error}")
            return
        self._wallet_book_changed()
        self._notice.setText(
            f"Añadida la cartera «{wallet.label}» ({shorten(wallet.address)}) en "
            f"modo observación: se leen sus saldos y no se puede firmar desde ella."
        )

    def _on_rename_wallet(self) -> None:
        """Renombra la cartera activa. La identidad sigue siendo la dirección."""
        keys = self._container.keys
        active = keys.active()
        if active is None:
            return
        texto, aceptado = QInputDialog.getText(
            self,
            "Renombrar cartera",
            "Nombre de la cartera (el número o el que quieras):",
            text=active.label,
        )
        if not aceptado:
            return
        nuevo = texto.strip()
        if not nuevo or nuevo == active.label:
            return
        try:
            renamed = keys.rename(active.wallet_id, nuevo)
        except Exception as error:
            self._notice.setText(f"No se renombró la cartera: {error}")
            return
        self._wallet_book_changed()
        self._notice.setText(
            f"La cartera {shorten(renamed.address)} ahora se llama «{renamed.label}»."
        )

    def _on_reveal_key(self) -> None:
        """Enseña la clave privada de la activa, siempre tras la contraseña.

        Se pide siempre, incluso con la sesión desbloqueada: es el único sitio de
        la aplicación donde una clave privada se pone en pantalla, y que
        enseñarla cueste un gesto deliberado es la diferencia entre una decisión
        y un accidente. Con la cartera heredada, además migra: la clave se cifra
        con esa contraseña y pasa al libro —la copia del llavero se queda—, y eso
        se dice al terminar.
        """
        keys = self._container.keys
        active = keys.active()
        if active is None:
            self._notice.setText("No hay ninguna cartera cuya clave enseñar.")
            return
        password = (
            self._ask_existing_password("Mostrar la clave privada")
            if keys.has_password()
            else self._ask_new_password()
        )
        if password is None:
            return
        try:
            private_key = keys.reveal(password)
        except WrongPasswordError as error:
            # Con la contraseña recién verificada no debería llegar aquí; se dice
            # igualmente porque éste es el sitio donde se puede leer el motivo.
            self._notice.setText(str(error))
            return
        except Exception as error:
            self._notice.setText(f"No se pudo enseñar la clave: {error}")
            return
        migrada = active.source is WalletSource.KEYRING
        RevealKeyDialog(active.label, private_key, parent=self).exec()
        if migrada:
            self._wallet_book_changed()
            self._notice.setText(
                f"La clave de «{active.label}» queda cifrada en el libro de "
                f"carteras. La copia del llavero no se ha tocado: sigue en "
                f"Credenciales como respaldo."
            )

    def _on_delete_wallet(self) -> None:
        """Elimina la cartera activa, tras confirmarlo con lo que se pierde."""
        keys = self._container.keys
        active = keys.active()
        if active is None:
            return
        dialogo = DeleteWalletDialog(active, parent=self)
        if dialogo.exec() != QDialog.Accepted or not dialogo.confirmed():
            return
        try:
            borrada = keys.remove(active.wallet_id)
        except Exception as error:
            self._notice.setText(f"No se eliminó la cartera: {error}")
            return
        self._wallet_book_changed()
        if not borrada:
            self._notice.setText("No se eliminó nada: la cartera ya no estaba en el libro.")
            return
        partes = [f"Eliminada la cartera «{active.label}» del libro."]
        if active.source is WalletSource.KEYRING:
            partes.append("La clave del llavero no se ha tocado: sigue en Credenciales.")
        despues = self._container.keys.active()
        partes.append(
            f"La activa ahora es «{despues.label}»."
            if despues is not None
            else "No queda ninguna: añade otra con el menú ☰."
        )
        self._notice.setText(" ".join(partes))

    def _on_toggle_lock(self) -> None:
        """Bloquea la sesión o la desbloquea, tras pedir la contraseña.

        El estado no se adivina con un booleano propio: se pregunta al proveedor
        con `is_unlocked`, que es el mismo que decide al firmar. Y desbloquear
        pasa por `unlock`, que verifica de verdad contra el almacén.
        """
        keys = self._container.keys
        if keys.is_unlocked():
            keys.lock()
            self._notice.setText(
                "Cartera bloqueada: la contraseña se ha olvidado y volverá a "
                "pedirse para firmar."
            )
            self._refresh_buttons()
            return
        if self._ask_existing_password("Desbloquear la cartera") is None:
            return
        self._notice.setText(
            "Cartera desbloqueada: se puede firmar sin volver a escribir la "
            "contraseña hasta cerrar la aplicación."
        )
        self._refresh_buttons()

    def _ask_new_password(self) -> str | None:
        """Crea la contraseña de las claves. Sin las dos escrituras, no hay nada.

        Sólo se llama cuando no hay ninguna clave cifrada todavía, así que este
        diálogo **es** la creación del almacén: avisa de que la contraseña no se
        guarda en ningún sitio recuperable, porque es la única vez que se puede
        decir antes de que importe.
        """
        dialogo = PasswordDialog(
            self,
            create=True,
            motivo=(
                "Protege las claves privadas que guarda este equipo. Se crea ahora "
                "y no se guarda en ningún sitio del que se pueda recuperar: si se "
                "pierde, estas claves no se pueden abrir."
            ),
        )
        if dialogo.exec() != QDialog.Accepted:
            return None
        return dialogo.password()

    def _ask_existing_password(self, motivo: str) -> str | None:
        """Pide la contraseña y no vuelve hasta que abra el almacén, o se cancele.

        La comprobación la hace `unlock`, que descifra de verdad un keystore: no
        hay un hash de la contraseña que comparar aparte, y no hace falta —el MAC
        del almacén ya responde a la única pregunta que importa—. Verificar aquí
        es también lo que deja la sesión desbloqueada, así que el gesto de
        desbloquear y el de confirmar son el mismo.
        """
        keys = self._container.keys
        error: str | None = None
        while True:
            dialogo = PasswordDialog(self, create=False, motivo=motivo, error=error)
            if dialogo.exec() != QDialog.Accepted:
                return None
            password = dialogo.password()
            if keys.unlock(password):
                return password
            error = (
                "Esa contraseña no abre el almacén de carteras: ninguna de las "
                "claves que guarda se abre con ella."
            )

    # ------------------------------------------------------------------ #
    # La lista de tokens y el detalle
    # ------------------------------------------------------------------ #
    def _build_tokens(self) -> QWidget:
        # El subtítulo se quita: explicaba el buscador, y el buscador ya lo dice en su
        # propio texto de ayuda. Una cabecera con título y subtítulo más tres controles
        # no cabía en el panel y se pisaban entre sí.
        card = Card("Tokens con saldo")
        # Los mandos de la lista, en la cabecera, «al lado del token»: la lupa
        # despliega el buscador, el «+» añade un token por la dirección de su
        # contrato y el botón de releer vuelve a pedir los saldos. Los tres
        # cambian la lista entera —y no una fila—, así que viven donde está su
        # título en vez de gastar una fila propia en un panel de 420 px.
        self._search_btn = QToolButton()
        self._search_btn.setObjectName("iconAction")
        self._search_btn.setCheckable(True)
        self._search_btn.setIcon(QIcon(_search_glyph()))
        self._search_btn.setIconSize(QSize(GLIF_PX, GLIF_PX))
        self._search_btn.setToolTip(
            "Buscar en toda la lista —también en la que está a cero— por símbolo, "
            "por nombre o por la dirección del contrato."
        )
        self._search_btn.setAccessibleName("Buscar")
        self._search_btn.toggled.connect(self._toggle_search)
        card.header.addWidget(self._search_btn)

        self._add_btn = QToolButton()
        self._add_btn.setObjectName("iconAction")
        self._add_btn.setIcon(QIcon(_plus_glyph()))
        self._add_btn.setIconSize(QSize(GLIF_PX, GLIF_PX))
        self._add_btn.setToolTip(
            "Añade un token por la dirección de su contrato. El símbolo y los "
            "decimales se leen de la cadena, no se piden a mano, y el token queda "
            "guardado para la próxima vez."
        )
        self._add_btn.setAccessibleName("Añadir token")
        self._add_btn.clicked.connect(self._on_add_token)
        card.header.addWidget(self._add_btn)

        self._refresh_btn = QToolButton()
        self._refresh_btn.setObjectName("iconAction")
        self._refresh_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self._refresh_btn.setIconSize(QSize(GLIF_PX, GLIF_PX))
        self._refresh_btn.setToolTip("Actualizar saldos y cotizaciones de la red")
        self._refresh_btn.setAccessibleName("Actualizar")
        self._refresh_btn.clicked.connect(self.refresh)
        card.header.addWidget(self._refresh_btn)

        # La lista y el detalle son dos vistas de la **misma tarjeta**, y se
        # turnan escondiendo una y enseñando la otra. No se usa un
        # `QStackedWidget` porque reserva el alto del hijo más grande: en un
        # panel de 420 px, el detalle dejaría un hueco de la altura de la lista
        # debajo. Esconder no reserva nada.
        self._list_view = self._build_list_view()
        self._detail_view = self._build_detail_view()
        self._detail_view.setVisible(False)
        card.body().addWidget(self._list_view)
        card.body().addWidget(self._detail_view)
        return card

    def _build_list_view(self) -> QWidget:
        vista = QWidget()
        lay = QVBoxLayout(vista)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # El buscador nace **plegado**: la lupa de la cabecera lo despliega. Es
        # un filtro, no un campo de cada día, y plegado devuelve su alto a la
        # lista, que es lo que se viene a ver.
        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar por nombre, símbolo o dirección…")
        self._search.setClearButtonEnabled(True)
        self._search.setToolTip(
            "Busca en toda la lista —también en la que está a cero— por símbolo, por "
            "nombre o por la dirección del contrato. Si lo que pegas no está, se "
            "ofrece añadirlo."
        )
        self._search.setVisible(False)
        lay.addWidget(self._search)

        # El selector de red: una línea de texto con su triángulo que abre el
        # modal. Sin desplegable incrustado —el filtro no es un campo de
        # formulario— y sin campo en la cabecera de la cartera: vive aquí, que
        # es donde filtra.
        self._chain_btn = QPushButton("▼ Todas las redes")
        self._chain_btn.setObjectName("link")
        self._chain_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chain_btn.clicked.connect(self._on_pick_chain)
        self._paint_chain_btn()
        lay.addWidget(self._chain_btn)

        self._only_positive = QCheckBox("Sólo con saldo")
        self._only_positive.setChecked(True)
        self._only_positive.setToolTip(
            "Desmárcalo para ver también los tokens de la lista que están a cero. "
            "Sirve para comprobar que un token está en la cartera aunque ahora no "
            "tenga nada."
        )
        lay.addWidget(self._only_positive)

        # Las filas, en su propio contenedor: el repintado las reconstruye
        # enteras y necesita un sitio del que poder quitarlas todas sin tocar el
        # resto de la vista.
        self._rows_host = QWidget()
        self._rows_box = QVBoxLayout(self._rows_host)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(2)
        lay.addWidget(self._rows_host)

        self._empty = QLabel("")
        self._empty.setObjectName("hint")
        self._empty.setWordWrap(True)
        self._empty.setVisible(False)
        lay.addWidget(self._empty)
        return vista

    def _build_detail_view(self) -> QWidget:
        """El detalle de un token: qué es, cuánto hay y qué se ha hecho con él.

        Todos los widgets se construyen una vez y se rellenan en
        `_refresh_detail`: el detalle puede quedar abierto mientras llega una
        lectura nueva, y volver a montarlo entero en cada repintado perdería el
        foco y el scroll —además de no servir de nada, porque los widgets son
        los mismos.
        """
        vista = QWidget()
        lay = QVBoxLayout(vista)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        volver = QPushButton("← Volver a la lista")
        volver.setObjectName("secondary")
        volver.clicked.connect(self._on_back)
        lay.addWidget(volver, 0, Qt.AlignLeft)

        cabecera = QHBoxLayout()
        cabecera.setSpacing(10)
        self._d_icon = QLabel()
        self._d_icon.setFixedSize(DETAIL_ICON_PX, DETAIL_ICON_PX)
        cabecera.addWidget(self._d_icon)

        nombres = QVBoxLayout()
        nombres.setSpacing(1)
        fila_nombre = QHBoxLayout()
        fila_nombre.setSpacing(2)
        self._d_name = QLabel("")
        self._d_name.setStyleSheet("font-size: 15px; font-weight: 700;")
        fila_nombre.addWidget(self._d_name)
        self._d_copy = _CopyButton()
        fila_nombre.addWidget(self._d_copy)
        fila_nombre.addStretch(1)
        nombres.addLayout(fila_nombre)
        self._d_contract = QLabel("")
        self._d_contract.setObjectName("hint")
        self._d_contract.setWordWrap(True)
        nombres.addWidget(self._d_contract)
        cabecera.addLayout(nombres, 1)
        lay.addLayout(cabecera)

        datos = QFormLayout()
        datos.setContentsMargins(0, 0, 0, 0)
        self._d_price = QLabel("—")
        self._d_amount = QLabel("—")
        self._d_value = QLabel("—")
        datos.addRow("Precio", self._d_price)
        datos.addRow("Cantidad", self._d_amount)
        datos.addRow("Valor", self._d_value)
        lay.addLayout(datos)

        acciones = QHBoxLayout()
        acciones.setSpacing(8)
        self._send_btn = QPushButton("Enviar")
        self._send_btn.clicked.connect(self._on_send)
        self._receive_btn = QPushButton("Recibir")
        self._receive_btn.setObjectName("secondary")
        self._receive_btn.setToolTip(
            "Muestra la dirección de la cartera en la red de este token, en texto "
            "y en código QR. No hay nada que firmar."
        )
        self._receive_btn.clicked.connect(self._on_receive)
        self._swap_btn = QPushButton("Swap")
        self._swap_btn.setObjectName("secondary")
        self._swap_btn.clicked.connect(self._on_detail_swap)
        acciones.addWidget(self._send_btn, 1)
        acciones.addWidget(self._receive_btn, 1)
        acciones.addWidget(self._swap_btn, 1)
        lay.addLayout(acciones)

        titulo = QLabel("Actividad")
        titulo.setStyleSheet("font-weight: 700;")
        titulo.setToolTip(
            "Lo que esta aplicación ejecutó con este token: envíos, swaps, "
            "puentes y sus recepciones, cobros y permisos. No es el histórico de "
            "la cadena: una transferencia que llegue de fuera no consta aquí."
        )
        lay.addWidget(titulo)

        self._activity_host = QWidget()
        self._activity_box = QVBoxLayout(self._activity_host)
        self._activity_box.setContentsMargins(0, 0, 0, 0)
        self._activity_box.setSpacing(8)
        lay.addWidget(self._activity_host)

        self._activity_empty = QLabel(
            "Aquí sólo consta lo que ejecuta esta aplicación: envíos, swaps, "
            "puentes, cobros y permisos hechos desde aquí. Lo que llegue de "
            "fuera no aparece."
        )
        self._activity_empty.setObjectName("hint")
        self._activity_empty.setWordWrap(True)
        lay.addWidget(self._activity_empty)
        return vista

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
                "No hay ninguna cartera configurada. Añádela con el menú ☰ de la "
                "cabecera —con su clave privada o en modo observación—, o pon la "
                "clave del llavero en la pestaña de Motores, en Credenciales."
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
        self._refresh_chain_keys(profile)
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
        # Se publican las redes leídas en la caché compartida **antes** de pintar y
        # antes de valorar: lo que la tarjeta de swap necesita de aquí son los
        # saldos, que ya están, no sus precios. Publicarlos ahora es lo que evita
        # que la tarjeta de al lado vuelva a preguntar al nodo por una red que esta
        # lectura acaba de traer entera.
        self._publish(snapshot)
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

    def _publish(self, snapshot: WalletSnapshot) -> None:
        """Deja las redes leídas en la caché compartida con la tarjeta de swap.

        Se publica **sólo** si la foto es de la cartera que firma. Una lectura de
        otra dirección —«Mirar otra…»— es un dato cierto de una cartera ajena, y
        dejarlo en la caché que usa el swap haría que la pantalla de al lado
        enseñara el dinero de otra persona bajo la dirección de la propia. La
        comprobación vive en `WalletBalances.publish`, que es donde se sabe quién
        es el dueño legítimo de la caché; aquí sólo se le pasa el dueño de esta
        lectura, que es la dirección del perfil leído.
        """
        for cadena in snapshot.chains:
            self._balances.publish(cadena.chain, cadena, owner=snapshot.profile.address)

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
        self._row_widgets = []
        # El detalle vuelve a la lista: lo que está mirando pertenece a la
        # cartera anterior, y dejarlo abierto bajo otra dirección sería enseñar
        # un saldo que ya no es el de nadie.
        self._detail_holding = None
        self._detail_view.setVisible(False)
        self._list_view.setVisible(True)
        _empty_layout(self._rows_box)
        self._total.setText("—")
        self._total_hint.setText("")
        self._state.setText("")
        # El selector vuelve a «Todas las redes» y sin opciones: sin perfil no
        # hay ninguna red que ofrecer, y dejar la anterior elegida filtraría la
        # cartera nueva por una red que ya no es la suya.
        self._chain_key = None
        self._chain_keys = ()
        self._paint_chain_btn()
        self._refresh_btn.setEnabled(True)
        self._refresh_buttons()

    def _profile_for(self, address: str) -> WalletProfile:
        """El perfil que se lee: la cartera activa, o la dirección que se pidió mirar.

        La familia se deduce por la **forma** de la dirección y no se pregunta: un
        `0x…` de 40 dígitos es EVM y un base58 de 32 bytes es Solana, y una
        dirección que no sea ninguna de las dos lanza aquí, que es donde se puede
        explicar.

        Si la dirección es la de la cartera activa, el perfil lleva **su**
        identidad —el identificador y el nombre reales, que pueden ser «2» o
        «Ahorros»—: con varias carteras, un «Mi cartera» fijo en el perfil haría
        que dos carteras distintas se pintaran igual.
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
        active = self._container.keys.active()
        mia = (self._container.keys.address() or "").lower()
        if active is not None and mia and mia == address.lower():
            return WalletProfile(
                wallet_id=active.wallet_id,
                label=active.label,
                kind=kind,
                address=address,
            )
        return WalletProfile(
            wallet_id=READONLY_WALLET_ID,
            label=READONLY_WALLET_LABEL,
            kind=kind,
            address=address,
        )

    def _refresh_chain_keys(self, profile: WalletProfile) -> None:
        """Las redes elegibles: las que esta cartera puede leer.

        Sólo las de su familia, y ni una más: ofrecer Polygon para una cartera de
        Solana sería ofrecer una consulta que siempre falla. Se guardan como
        opciones —las que enseña el modal del selector— y, si la red elegida ya
        no está entre ellas, el filtro vuelve a «Todas»: quedarse filtrando por
        una red que esta cartera no cubre escondería todas las filas sin decir
        por qué.
        """
        self._chain_keys = tuple(
            key for key in profile.chains_of() if key in self._engine_chains()
        )
        if self._chain_key is not None and self._chain_key not in self._chain_keys:
            self._set_chain(None)
            return
        self._paint_chain_btn()

    def _set_chain(self, key: str | None) -> None:
        """Fija el filtro de red y repinta las filas, sin volver a leer nada."""
        self._chain_key = key
        self._paint_chain_btn()
        self._repaint_rows()

    def _paint_chain_btn(self) -> None:
        if self._chain_key is None:
            self._chain_btn.setText("▼ Todas las redes")
            self._chain_btn.setToolTip(
                "La lista enseña todas las redes que se leyeron de esta cartera. "
                "Pulsa para elegir una."
            )
            return
        nombre = _chain_name(self._chain_key)
        self._chain_btn.setText(f"▼ Red: {nombre}")
        self._chain_btn.setToolTip(
            f"Sólo se enseñan los saldos de {nombre}. Pulsa para cambiar de red "
            f"o volver a «Todas las redes»."
        )

    def _on_pick_chain(self) -> None:
        """Abre el modal de redes y aplica lo que se elija.

        Se abre con las claves que esta cartera puede leer —las del último
        perfil— y no con el catálogo: es la misma frontera que ya aplica la
        lectura, enseñada en el selector para no ofrecer lo que fallaría.
        """
        dialogo = ChainPickerDialog(self._chain_keys, self._chain_key, parent=self)
        if dialogo.exec() != QDialog.Accepted:
            return
        self._set_chain(dialogo.chosen())

    def _toggle_search(self, activo: bool) -> None:
        """La lupa despliega el buscador; al plegarlo, lo vacía.

        Vaciar al plegar no es un detalle: un filtro escondido junto a su campo
        es un filtro que desaparece sin decirlo, y las filas que faltaran no
        tendrían por qué. Lo que se ve es lo que está puesto.
        """
        self._search.setVisible(activo)
        if activo:
            self._search.setFocus()
            return
        self._search.clear()

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
        """Reconstruye la lista con las filas que pasan los tres filtros: red,
        saldo y buscador.

        Los tres se aplican aquí y no en la lectura porque los tres se pueden
        cambiar sin volver a preguntar a un nodo, y volver a preguntar por un
        cambio de filtro sería gastar ocho llamadas de red para esconder una fila.

        Las filas se **reconstruyen** en vez de actualizarse una a una: cada
        repintado llega con una lectura nueva, y casar fila vieja con holding
        nuevo exigiría reconocer los tokens por dentro —que es justo lo que
        `_TokenRow` ya guarda— mientras que reconstruir siempre enseña lo que hay
        y no puede quedarse con una fila huérfana.
        """
        filtro = self._search.text().strip().lower()
        red = self._chain_key
        solo_con_saldo = self._only_positive.isChecked()

        visibles = [
            (cadena, holding)
            for cadena, holding in self._rows
            if (red is None or cadena.chain == red)
            and (not solo_con_saldo or not holding.is_empty)
            and self._matches(holding, filtro)
        ]

        _empty_layout(self._rows_box)
        self._row_widgets = []
        for _cadena, holding in visibles:
            fila = _TokenRow(holding, self._rows_host)
            fila.clicked.connect(self._open_detail)
            self._row_widgets.append(fila)
            self._rows_box.addWidget(fila)

        vacio = not visibles and bool(self._rows)
        self._empty.setVisible(vacio)
        if vacio:
            self._empty.setText(self._empty_text(filtro, red))

        # Un detalle abierto se recuelga de la lectura recién pintada —o vuelve
        # a la lista si su token ya no está—: dejarlo con la foto anterior
        # enseñaría un saldo que puede haber cambiado en esta misma lectura.
        if self._detail_holding is not None:
            self._refresh_detail()

    def _empty_text(self, filtro: str, red: str | None) -> str:
        if filtro and self._looks_like_contract(filtro):
            return (
                f"«{filtro}» no aparece con saldo. Si es la dirección de un token "
                f"que tienes, añádelo con el «+» de la cabecera: la lista del "
                f"catálogo es finita y un token nuevo no puede estar en ella."
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
                f"No hay saldos en {CHAINS[red].name} para esta dirección. Cambia "
                f"de red en el selector, o mira «Todas las redes»."
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

    # ------------------------------------------------------------------ #
    # El detalle de un token
    # ------------------------------------------------------------------ #
    def _open_detail(self, holding: TokenHolding) -> None:
        """Pasa de la lista al detalle de **ese** token.

        Se guarda el holding, no sus cifras: `_refresh_detail` lo vuelve a
        resolver contra la última lectura antes de pintar, así que lo que se
        enseña es siempre el saldo de ahora y no el de cuando se pulsó la fila.
        """
        self._detail_holding = holding
        self._list_view.setVisible(False)
        self._detail_view.setVisible(True)
        self._refresh_detail()

    def _on_back(self) -> None:
        self._detail_holding = None
        self._detail_view.setVisible(False)
        self._list_view.setVisible(True)

    def _refresh_detail(self) -> None:
        holding = self._detail_holding
        if holding is None:
            return
        # El token puede haber desaparecido de la última lectura —cambió la
        # cartera, o esa red ya no se lee— y quedarse con la foto vieja bajo la
        # lectura nueva sería enseñar un saldo que ya no es el que hay. Cuando
        # pasa, el detalle se cierra solo y se vuelve a la lista.
        actual = next(
            (fila for _, fila in self._rows if _holding_key(fila) == _holding_key(holding)),
            None,
        )
        if actual is None:
            self._on_back()
            return
        self._detail_holding = actual
        token = actual.token
        red = _chain_name(token.chain)

        self._d_icon.setPixmap(badged_token_pixmap(token, size=DETAIL_ICON_PX))
        # El nombre, a secas: el contrato se dice en su propia línea de abajo, y
        # repetir la dirección aquí era decir dos veces lo mismo con el mismo
        # ancho. El desempate de homónimos queda en el tooltip del contrato.
        self._d_name.setText(token.display_symbol)
        if token.address:
            self._d_copy.setVisible(True)
            self._d_copy.set_address(token.address)
            self._d_contract.setText(f"Contrato {shorten(token.address)}")
            self._d_contract.setToolTip(
                f"{token.address}\n{red}, {token.decimals} decimales."
            )
        else:
            # Sin contrato no hay nada que copiar, y un botón de copiar que
            # copia la cadena vacía no es un botón: se esconde.
            self._d_copy.setVisible(False)
            self._d_contract.setText(
                f"Moneda nativa de {red}: no tiene contrato."
            )
            self._d_contract.setToolTip("")

        cantidad = actual.as_decimal()
        self._d_amount.setText(f"{_format_total(cantidad)} {token.symbol}")
        self._d_amount.setToolTip(f"{cantidad:f} {token.symbol}")
        referencia = actual.value_in_reference
        texto_valor, ayuda_valor = _value_text(actual)
        self._d_value.setText(texto_valor)
        self._d_value.setToolTip(ayuda_valor)
        if actual.is_empty:
            self._d_price.setText("—")
            self._d_price.setToolTip(
                "No hay saldo que valorar, así que no hay precio que calcular "
                "—y no se preguntó a ningún motor por él—."
            )
        elif referencia is None:
            self._d_price.setText("sin cotización")
            self._d_price.setToolTip(
                "El precio por unidad sale de dividir el valor de la posición "
                "entre su cantidad, y esa posición no se pudo valorar."
            )
        else:
            # Derivado, no publicado: es el valor de la posición dividido entre
            # sus unidades. Con `format_amount` y no con dos decimales porque un
            # precio por unidad vive en otra escala que un saldo —el de un
            # token diminuto se leería «0.00»—.
            precio = referencia.as_decimal() / cantidad
            self._d_price.setText(f"{format_amount(precio)} {referencia.symbol}")
            self._d_price.setToolTip(
                f"El valor de la posición entre sus unidades: "
                f"{referencia.as_decimal():f} {referencia.symbol} por "
                f"{format_amount(cantidad, full=True)} {token.symbol}."
            )

        self._refresh_detail_buttons()
        self._fill_activity(actual)

    def _engines_of(self, kind: EngineKind) -> frozenset[str]:
        """Los identificadores de los motores que ocupan una ranura.

        Se preguntan al registro en vez de escribirse a mano para que un motor
        nuevo de swap o de puente salga bien etiquetado sin tocar esto. Si el
        registro no respondiera, la lista vacía deja «Movimiento» como etiqueta,
        que es lo que se dice de lo que no se sabe.
        """
        try:
            return frozenset(
                registered.engine_id
                for registered in self._container.registry.available(kind)
            )
        except Exception:  # pragma: no cover - el registro no lanza hoy
            return frozenset()

    def _fill_activity(self, holding: TokenHolding) -> None:
        """Los asientos del registro que tocan este token, del más nuevo al más viejo.

        Es la lista de lo que **esta aplicación** ejecutó con él, no el histórico
        de la cadena: la fuente es `executions.jsonl`. Una transferencia que
        llegue de fuera no está —y no puede estarlo—, y por eso el texto de
        vacío lo dice con esas palabras en vez de dejar creer que no pasó nada.
        """
        _empty_layout(self._activity_box)
        token = holding.token
        entradas = self._container.policy.ledger.for_token(token.symbol, chain=token.chain)
        self._activity_empty.setVisible(not entradas)
        if not entradas:
            return
        puentes = self._engines_of(EngineKind.CROSS_CHAIN)
        swaps = self._engines_of(EngineKind.DEX_QUOTES)
        for entry in entradas:
            self._activity_box.addWidget(self._activity_row(entry, bridges=puentes, dexes=swaps))

    def _activity_row(
        self,
        entry: LedgerEntry,
        *,
        bridges: frozenset[str],
        dexes: frozenset[str],
    ) -> QWidget:
        fila = QWidget()
        lay = QVBoxLayout(fila)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)

        arriba = QHBoxLayout()
        arriba.setSpacing(6)
        chip = Chip(_entry_label(entry, bridges=bridges, dexes=dexes), _entry_color(entry))
        arriba.addWidget(chip)
        que = QLabel(entry.pair)
        que.setWordWrap(True)
        arriba.addWidget(que, 1)
        cuando = QLabel(f"{entry.occurred_at:%d/%m %H:%M}")
        cuando.setObjectName("hint")
        cuando.setToolTip(f"{entry.occurred_at:%Y-%m-%d %H:%M:%S}")
        arriba.addWidget(cuando)
        lay.addLayout(arriba)

        detalles = _activity_lines(entry)
        if detalles:
            linea = QLabel(" · ".join(detalles))
            linea.setObjectName("hint")
            linea.setWordWrap(True)
            lay.addWidget(linea)

        fila.setToolTip(
            f"{entry.pair}\n{entry.occurred_at:%Y-%m-%d %H:%M:%S}\n"
            f"Importe para el tope: {entry.notional} {entry.notional_symbol}\n"
            f"{entry.description}\nHash: {entry.tx_hash}"
        )
        return fila

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
    def _withdraw_blocked_reason(self) -> str | None:
        """Por qué no se puede retirar ahora mismo, o `None` si sí.

        Es **una** respuesta para los dos botones que retiran —«Retirar» de la
        cabecera y «Enviar» del detalle—: dos copias de estas comprobaciones se
        desviarían, y la copia que se desvía es la que deja firmar donde no se
        podía.

        Los tres modos de «no se puede» de la cartera activa —ninguna, en
        observación, bloqueada— se dicen por separado porque se arreglan en
        sitios distintos: añadir una cartera, elegir la que tiene clave,
        desbloquear. Un motivo genérico mandaría a buscar la solución donde no
        está.
        """
        perfil = self._profile
        if perfil is None:
            return "No hay ninguna cartera que mirar."
        # La familia **antes** que la dirección, y el orden no es indiferente: una
        # cartera de Solana no se puede retirar desde aquí ni con la clave
        # correcta, así que decirle a quien la mira que «no es la que firma» sería
        # mandarlo a cambiar de clave para volver a chocar con lo mismo.
        if perfil.kind is WalletKind.SOLANA:
            return (
                "Retirar en Solana todavía no está implementado: la transacción se "
                "construye de otra forma y la comisión se paga en otra unidad. La "
                "pestaña de swap sí cotiza en Solana."
            )
        keys = self._container.keys
        active = keys.active()
        if active is None:
            return (
                "No hay ninguna cartera con la que firmar. Añádela con el menú ☰ "
                "de la cabecera, con su clave privada: una cartera en modo "
                "observación sólo se lee."
            )
        if active.source is WalletSource.WATCH:
            return (
                f"«{active.label}» está en modo observación: se leen sus saldos, "
                f"pero no hay ninguna clave desde la que firmar. Pulsa su nombre, "
                f"arriba, y elige una cartera con clave."
            )
        if keys.is_locked():
            return (
                f"La cartera «{active.label}» está bloqueada: hay que escribir la "
                f"contraseña de las claves para firmar. Desbloquéala en el menú ☰ "
                f"de la cabecera («Desbloquear cartera»)."
            )
        if not keys.available():
            return (
                "No hay ninguna clave privada utilizable, así que no hay desde "
                "dónde retirar. Ponla en la pestaña de Motores, en Credenciales, "
                "o añade una cartera con clave desde el menú ☰."
            )
        mia = (keys.address() or "").lower()
        if mia != perfil.address.lower():
            return (
                f"Estás mirando {shorten(perfil.address)}, que no es la cartera "
                f"activa ({shorten(mia)}). Se puede depositar —una dirección "
                f"pública no necesita clave— pero no retirar: firmar desde otra "
                f"cartera mandaría el dinero desde donde no estás mirando."
            )
        return None

    _WITHDRAW_TOOLTIP: Final = (
        "Saca un token de la cartera hacia otra dirección. Firma y emite: es "
        "una operación real e irreversible."
    )

    def _refresh_buttons(self) -> None:
        """Quién puede hacer qué, y por qué no.

        Depositar funciona siempre que haya una cartera que enseñar: es una
        dirección pública. Retirar necesita que la activa **pueda firmar** —una
        clave utilizable, la sesión desbloqueada si está cifrada, y que la
        cartera mirada sea la que esa clave abre, en una red EVM— y todo se
        comprueba al pintar, no al pulsar. Un botón que se puede pulsar y falla
        enseña a desconfiar de los botones; uno apagado con su motivo al lado
        enseña qué falta.
        """
        self._deposit_btn.setEnabled(self._profile is not None)
        motivo = self._withdraw_blocked_reason()
        self._withdraw_btn.setEnabled(motivo is None)
        self._withdraw_btn.setToolTip(motivo or self._WITHDRAW_TOOLTIP)
        self._refresh_detail_buttons()

    def _refresh_detail_buttons(self) -> None:
        """Los tres botones del detalle, con el motivo del botón de la cabecera.

        «Recibir» no se apaga nunca: enseñar una dirección pública no necesita
        nada. «Swap» se apaga donde la conversión no existe, que es la misma
        condición que cumplía el ⇄ de la fila al que sustituye.
        """
        motivo = self._withdraw_blocked_reason()
        self._send_btn.setEnabled(motivo is None)
        self._send_btn.setToolTip(
            motivo
            or (
                "Envía este token a otra dirección: abre el mismo formulario que "
                "«Retirar», con este token ya elegido. Firma y emite, y no se "
                "puede deshacer."
            )
        )
        holding = self._detail_holding
        if holding is None:
            return
        en_catalogo = holding.token.chain in CHAINS
        self._swap_btn.setEnabled(en_catalogo)
        self._swap_btn.setToolTip(
            f"Lleva {holding.token.symbol} a la tarjeta de conversión, en "
            f"{_chain_name(holding.token.chain)}, sin volver a buscarlo en la lista."
            if en_catalogo
            else f"{_chain_name(holding.token.chain)} no se puede convertir todavía."
        )

    def _on_deposit(self) -> None:
        perfil = self._profile
        if perfil is None:
            return
        red = self._chain_key
        if red is None:
            red = next(iter(perfil.chains_of()), None)
        if red is None:
            self._notice.setText("Esta cartera no cubre ninguna red del catálogo.")
            return
        DepositDialog(perfil.address, red, self).exec()

    def _on_receive(self) -> None:
        """«Recibir» del detalle: la dirección de la cartera **en la red del token**.

        Es el mismo diálogo que «Depositar», con la red fijada a la del token:
        es la única que tiene sentido desde su detalle, y enseñar la dirección
        con otra red seleccionada sería invitar a mandar el token por la red
        equivocada —que es justo lo que el aviso del diálogo existe para
        impedir—.
        """
        holding = self._detail_holding
        perfil = self._profile
        if holding is None or perfil is None:
            return
        DepositDialog(perfil.address, holding.token.chain, self).exec()

    def _on_detail_swap(self) -> None:
        holding = self._detail_holding
        if holding is None or holding.token.chain not in CHAINS:
            return
        self.swap_requested.emit(holding.token)

    def _on_withdraw(self) -> None:
        """«Retirar» de la cabecera: la red del desplegable, sin token prefijado."""
        perfil = self._profile
        if perfil is None:
            return
        red = self._chain_key
        cadena = red or (perfil.chains_of()[0] if perfil.chains_of() else None)
        if cadena is None:
            return
        self._open_withdraw(cadena, None)

    def _on_send(self) -> None:
        """«Enviar» del detalle: este token, en su red, ya elegido."""
        holding = self._detail_holding
        if holding is None:
            return
        self._open_withdraw(holding.token.chain, holding.token)

    def _open_withdraw(self, cadena: str, por_defecto: Token | None) -> None:
        """El formulario de retirada, con o sin token elegido de antemano.

        Lo comparten «Retirar» —la red del desplegable— y «Enviar» del detalle
        —la red del token, y el token ya puesto—. El diálogo no firma ni
        comprueba topes: devuelve lo que el usuario escribió y el caso de uso
        hace el resto en su orden —modo, red, destino, topes, saldo, permiso,
        firma—. Duplicar aquí una parte de ese orden sería tener dos versiones
        de la misma regla, y la que se desvía es siempre la que no se ejecuta.
        """
        perfil = self._profile
        if perfil is None:
            return
        tokens = tokens_for_chain(self._container.token_store, cadena)
        if por_defecto is not None and all(
            not conocido.is_same_asset(por_defecto) for conocido in tokens
        ):
            # El token del detalle puede no estar en el catálogo —se añadió a
            # mano, o vive fuera de la lista de tokens conocidos—: se ofrece
            # igualmente, en vez de abrir el formulario sin el token que el
            # usuario acaba de mirar. El caso de uso lo validará como a
            # cualquier otro.
            tokens = (por_defecto, *tokens)
        if not tokens:
            self._notice.setText(f"No hay tokens conocidos en {_chain_name(cadena)}.")
            return
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
        self._send_btn.setEnabled(False)
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
        """Mira una dirección sin guardarla. Para volver, el nombre de la cartera."""
        direccion, aceptado = QInputDialog.getText(
            self,
            "Mirar otra cartera",
            "Dirección a consultar (EVM «0x…» o Solana en base58):\n"
            "(se leen sus saldos; sin su clave no se puede firmar desde ella)",
        )
        if not aceptado or not direccion.strip():
            return
        self._set_address_text(direccion.strip())
        self._profile = None
        self._snapshot = None
        self.refresh()

    def _on_add_token(self) -> None:
        """Añade un token por su dirección, en la red elegida.

        Se resuelve contra el contrato —símbolo y decimales se **leen**, no se
        piden— y queda guardado en el almacén, que es lo que hace que el token
        siga en la lista la próxima vez y que aparezca también en las tarjetas de
        swap y de puentes.

        Con «Todas las redes» puesto, la red se pregunta en el mismo gesto: la
        dirección de un contrato sólo significa algo dentro de su red, así que
        sin red no hay nada que leer. Antes sólo salía un aviso que obligaba a ir
        al selector, elegir la red y volver a pulsar el «+».
        """
        red = self._chain_key
        if red is None:
            if not self._chain_keys:
                self._notice.setText(
                    "No hay ninguna red que leer en este momento: elige antes una "
                    "cartera con una dirección válida."
                )
                return
            elegir = ChainPickerDialog(self._chain_keys, self._chain_keys[0], parent=self)
            elegir.setWindowTitle("¿En qué red está el token?")
            if elegir.exec() != QDialog.Accepted:
                return
            red = elegir.chosen()
            if red is None:
                self._notice.setText(
                    "La dirección de un contrato sólo significa algo dentro de su "
                    "red: elige una red concreta para añadir el token."
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
