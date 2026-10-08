"""Widgets reutilizables de la UI.

Lo que hay aquí es lo que aparece en más de una pestaña, y sólo eso: una tarjeta,
una etiqueta de estado, una fila de campo con su nombre, el diálogo de
confirmación y el banner de alertas. Un widget que sólo usa una pestaña vive en
esa pestaña, aunque parezca genérico — «genérico» con un solo usuario es una
carpeta de cajón, no una abstracción.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.confirmation import PendingAction
from amigocompora.domain.models import PlannedTransaction, Token
from amigocompora.domain.modes import Capability
from amigocompora.engines.catalog import native_token, tokens_for
from amigocompora.engines.token_store import UserTokenStore
from amigocompora.ui.theme import (
    COLOR_BG,
    COLOR_BORDER,
    COLOR_BORDER_STRONG,
    COLOR_DANGER,
    COLOR_ELEVATED,
    COLOR_MUTED,
    COLOR_TEXT,
    COLOR_WARNING,
)

#: Un payload en base64 o un calldata ocupan cientos de caracteres y no caben en
#: el diálogo. Se recorta sólo para **mostrar**: el valor completo sigue en el
#: objeto, que es lo que se exporta.
_CLIP_CHARS: Final = 120


def _clip(value: str) -> str:
    return value if len(value) <= _CLIP_CHARS else f"{value[:_CLIP_CHARS]}…"


#: Cifras significativas que se enseñan de una cantidad antes de recortarla.
#: Seis es lo que cabe sin que la columna baile y lo que distingue 0,000001 de
#: 0,0000012 —que es la diferencia entre un polvo y un polvo con algo dentro—.
SIGNIFICANT_DIGITS: Final = 6


def format_amount(value: Decimal, *, full: bool = False) -> str:
    """Una cantidad, recortada a lo que se puede leer.

    Vive aquí porque la usan dos pantallas que enseñan los mismos saldos —la
    cartera y la tarjeta de conversión— y dos recortes distintos para el mismo
    número serían dos verdades distintas sobre lo que hay en la cuenta.

    Con `full` se devuelve todo, y ése es el otro modo que tiene la cartera: la
    cifra recortada es la que se mira y la completa es la que se comprueba. Se
    recorta por **dígitos significativos** y no por decimales porque una cartera
    tiene las dos cosas: 0,0000012 ETH perdería todo su contenido con dos
    decimales, y 19,491666140000000 POL no gana nada con dieciocho.

    Lo que no se hace nunca es redondear a cero: un saldo con fondos que se
    imprime como «0» no es una cifra imprecisa, es una cifra **falsa**, y ésa sí
    se copia y se decide con ella. Por eso los dos extremos —el polvo y la
    cantidad larga— se escriben enteros en vez de recortarse.

    El recorte es **sólo de la pantalla**. Lo que se firma sale del `TokenAmount`
    completo, nunca de este texto: por eso la función devuelve una cadena y no
    toca el número que recibe.
    """
    if full:
        return f"{value:f}"
    if value == 0:
        return "0"
    texto = f"{value:.{SIGNIFICANT_DIGITS}g}"
    # `g` decide por su cuenta entre notación normal y científica, y se pasa a la
    # científica sólo en los dos extremos: la cantidad muy larga («1.23457e+6») y
    # el polvo («1e-7»). Para un saldo no sirve ninguna de las dos: la primera no
    # se lee, y la segunda se confunde con un cero justo cuando el número dice
    # que hay algo. Las dos se escriben enteras.
    return _plain(value) if ("e" in texto or "E" in texto) else texto


def _plain(value: Decimal) -> str:
    """La cantidad en notación normal, sin perder ninguna cifra por el camino.

    Los decimales se cuentan desde el exponente del propio número y no se eligen a
    ojo: bajar `1,234e-7` con los seis decimales de la pantalla daría «0.000000»,
    que es exactamente el cero que se venía a evitar. Con los decimales que hacen
    falta para sus cifras significativas, el polvo se lee entero y la cantidad
    larga —que no cabe con decimales— se redondea al entero.
    """
    ajustado = value.adjusted()
    if ajustado >= SIGNIFICANT_DIGITS:
        return f"{value:.0f}"
    return f"{value:.{-ajustado + SIGNIFICANT_DIGITS - 1}f}".rstrip("0").rstrip(".")


#: El aviso del diálogo, según lo que la acción va a hacer **de verdad**.
#:
#: Estaba escrito a mano —«Amigocompora nunca firma ni emite transacciones»— y
#: con la ejecución real esa frase pasó a ser falsa. Es la línea que más importa
#: de todo el diálogo, porque dice justo lo contrario de lo que va a ocurrir en
#: el instante exacto en que alguien decide si ocurre. La capacidad viaja dentro
#: de la acción, así que el aviso puede depender de ella en vez de suponerla.
#:
#: Sólo hay dos textos porque sólo hay dos capacidades confirmables
#: (`CONFIRMABLE_CAPABILITIES`, en `domain/modes.py`): construir el payload y
#: emitirlo. No hay una tercera rama esperando, y por eso no hay un `else` que
#: adivine.
_SIGNING_WARNING: Final = (
    "Esta operación es <b>real e irreversible</b>. Amigocompora va a <b>firmar y "
    "emitir</b> una transacción a la red: sale dinero de tu cartera y, una vez "
    "emitida, no se puede deshacer ni cancelar. Revisa la red, el importe y el "
    "destino."
)

_PREPARE_WARNING: Final = (
    "Confirmar aquí sólo autoriza a <b>construir</b> el payload. En esta acción "
    "Amigocompora no firma ni emite nada."
)


def warning_for(capability: Capability) -> tuple[str, str]:
    """El texto del aviso y su color, para la capacidad que se está confirmando."""
    if capability is Capability.BROADCAST_TX:
        return _SIGNING_WARNING, COLOR_DANGER
    return _PREPARE_WARNING, COLOR_WARNING


#: Tareas de fondo vivas. Guardar la referencia evita que el recolector de
#: basura cancele una corrutina recién lanzada desde un handler de Qt.
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Lanza una corrutina desde la UI y conserva su referencia hasta que acaba."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def divider() -> QFrame:
    """Una línea de separación de un píxel, con el color del borde."""
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return line


def tokens_for_chain(store: UserTokenStore, chain_key: str) -> tuple[Token, ...]:
    """Los tokens elegibles en una red: el nativo, el catálogo y los añadidos.

    Vive aquí —y no en la pestaña que la estrenó— porque ya la usan tres, y porque
    las tres tienen que decir lo mismo: un token guardado por su contrato tiene que
    aparecer igual en la tarjeta de swaps, en la de puentes y en el diálogo de
    retirada. «Guardarlo para que aparezca en la lista» no dice en cuál de ellas.

    **La moneda de la red va primero**, y no es un detalle de orden: es lo que una
    cartera tiene casi siempre, lo que paga el gas y lo único que se mueve sin
    aprobación. Faltaba, y el hueco se notaba en dos sitios a la vez: retirar ETH
    era imposible desde la interfaz aunque el caso de uso lo soporta entero —con
    la reserva de gas, que sólo tiene sentido en el nativo—, y «intercambiar este
    token» desde la cartera no encontraba dónde poner el POL de una cuenta. Los
    motores ya lo aceptaban: Uniswap lo cotiza por su envoltorio y los puentes
    distinguen el origen nativo. El único que no lo tenía era esta lista.

    Se quitan los repetidos por `(símbolo, dirección)` y no por símbolo: en Polygon
    el USDC nativo y el puenteado desde Ethereum publican el mismo símbolo, y son
    dos tokens distintos —uno de ellos es el colateral que acepta Polymarket—.
    """
    nativo = native_token(chain_key)
    found = [nativo, *tokens_for(chain_key)]
    conocidos = {(token.symbol, token.address) for token in found}
    found.extend(
        token
        for token in store.load()
        if token.chain == chain_key and (token.symbol, token.address) not in conocidos
    )
    return tuple(found)


def token_labels(tokens: tuple[Token, ...]) -> tuple[str, ...]:
    """Etiquetas de un desplegable de tokens, desempatando los homónimos.

    Dos tokens distintos pueden publicar el mismo símbolo, y repetir la etiqueta
    deja al usuario eligiendo a ciegas entre dos cosas que no son la misma. Sólo
    cuando un símbolo aparece más de una vez se le añade el principio de la
    dirección, que es lo que sí distingue; con símbolos únicos la etiqueta queda
    limpia, que es el caso normal.
    """
    repetidos = {
        token.symbol
        for token in tokens
        if sum(otro.symbol == token.symbol for otro in tokens) > 1
    }
    return tuple(
        token.qualified_symbol if token.symbol in repetidos else token.symbol
        for token in tokens
    )


def set_empty(table: QTableWidget, placeholder: QLabel, texto: str) -> None:
    """Enseña la tabla o su rótulo, nunca los dos.

    Vive aquí, y no en la página que lo estrenó, porque lo usan ya dos: una
    tabla vacía **no es pequeña** —ocupa su `stretch` entero—, así que un
    rótulo encima del rectángulo gris dejaría las dos cosas a la vez. Lo que se
    quiere es que el sitio de la tabla lo ocupe la explicación, que es lo que
    además distingue «todavía no has pedido nada» de «pediste y no hay nada».

    El texto entra por parámetro porque cada tabla tiene su propio «por qué está
    vacía», y ese es justamente el dato que no se puede generalizar.
    """
    vacia = table.rowCount() == 0
    placeholder.setText(texto)
    placeholder.setVisible(vacia)
    table.setVisible(not vacia)


def needed_width(control: QWidget) -> int:
    """El ancho que un campo necesita **de verdad** para enseñar su contenido.

    Existe porque el ancho se pide en el sitio equivocado. `sizeHint()` de un
    campo se calcula con la fuente, y la fuente la pone la hoja de estilos de la
    aplicación —que se instala en el `QApplication`, y no está aplicada todavía
    cuando el widget se construye—. Medido: con el estilo sin aplicar el mismo
    `QDoubleSpinBox` declara 225 px, y el texto que tiene que enseñar pide 182
    más el relleno. En Windows coincide por casualidad; en una máquina con la
    fuente por omisión más estrecha no coincidiría, y el campo se recortaría.

    Se fuerza el pulido con la hoja ya puesta y **después** se pregunta, que es
    el único orden que da la medida buena. Desde Qt 6.0 el aviso de que
    `sizeHint()` se consulta antes de pulir dejó de emitirse, así que el fallo no
    se ve en el log: se ve como una columna más estrecha que su contenido.
    """
    control.ensurePolished()
    return max(control.sizeHint().width(), control.minimumSizeHint().width())


def width_for_chars(control: QWidget, chars: int) -> int:
    """El ancho que hace falta para enseñar `chars` caracteres en ese control.

    Existe porque el ancho natural de un `QComboBox` es el de su **entrada más
    larga**, y una lista de once redes con sus claves mide cientos de píxeles: en
    una columna de 340 px eso deja el campo más estrecho que el texto que muestra.
    `setMinimumContentsLength` es la herramienta de Qt para eso, pero sólo actúa
    cuando hay **más** entradas que caracteres: con una sola entrada —«Todas las
    redes», que es lo que hay mientras la cartera no se ha leído— Qt vuelve a medir
    el texto y el campo se queda en 96 px pidiendo 218.

    Esto da un suelo calculado con la fuente **ya aplicada**, que es lo único que se
    puede afirmar sin depender de qué fuente tenga la máquina. Se mide una «M» y se
    multiplica: es una aproximación por exceso para la mayoría de las letras, que es
    la dirección correcta del error en un campo que se recorta.
    """
    control.ensurePolished()
    metrics = QFontMetrics(control.font())
    return metrics.horizontalAdvance("M" * chars) + 34  # margen + galón del combo


class ScrollArea(QWidget):
    """El armazón de una pestaña: contenido que se desplaza en vez de recortarse.

    Existe por el defecto más visible que ha tenido esta interfaz. Ninguna página
    se desplazaba y la ventana no baja de 1100x720, así que lo que no cabía **se
    cortaba**: los botones de «Entre redes» aparecían amputados a media altura,
    tres campos de Predicción se quedaban en 133 px cuando necesitaban 140, la
    tarjeta de orden salía aplastada contra su ancho máximo y la tabla de motores
    enseñaba una fila de quince dentro de noventa píxeles. No era un problema de
    una pantalla concreta: era que ninguna sabía qué hacer cuando el contenido no
    cabe, y el sitio donde eso se arregla una vez es aquí.

    `body()` devuelve el layout del contenido **de verdad**, no el del armazón, así
    que las páginas se siguen construyendo igual: sólo cambia dónde ponen las
    cosas, y ningún widget ni atributo cambia de nombre.

    La barra horizontal va apagada a propósito: el contenido se reparte a lo ancho,
    y una barra horizontal es la señal de que algo no se está repartiendo bien.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 12) -> None:
        super().__init__(parent)
        self.setObjectName("scrollArea")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        contenido = QWidget()
        contenido.setObjectName("scrollContent")
        self._body = QVBoxLayout(contenido)
        # Margen derecho para que la barra de desplazamiento no tape nada cuando
        # aparece, y ninguno a la izquierda: la tarjeta ya lleva el suyo.
        self._body.setContentsMargins(0, 0, 6, 0)
        self._body.setSpacing(spacing)
        self._scroll.setWidget(contenido)
        outer.addWidget(self._scroll)

    def body(self) -> QVBoxLayout:
        """El layout donde van las tarjetas de la página."""
        return self._body

    @classmethod
    def fill(cls, page: QWidget, *, spacing: int = 12) -> ScrollArea:
        """Monta el armazón como único contenido de una página, ya estirado.

        Son cuatro líneas de layout exterior idénticas en las seis pestañas, y
        escribirlas seis veces es la forma segura de que una acabe con márgenes
        distintos. La página se pasa por parámetro porque es la que manda en el
        tamaño; el armazón es lo único que va dentro.
        """
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        armazon = cls(page, spacing=spacing)
        outer.addWidget(armazon)
        return armazon

    def scroll_to(self, widget: QWidget) -> None:
        """Lleva la vista hasta un widget, sin saltar de pestaña.

        Es lo que sustituye al cambio de pestaña que hacía «Intercambiar este
        token» cuando la cartera vivía en otra pantalla: ahora comparten scroll, y
        lo que falta no es irse a otro sitio sino llegar al sitio.
        """
        self._scroll.ensureWidgetVisible(widget, 0, 12)


class Card(QFrame):
    """Contenedor con borde y fondo de tarjeta, con cabecera opcional.

    La cabecera existe para que el título de una sección no sea un `QLabel` en
    negrita colgado del layout: puesto aquí, todas las tarjetas de la aplicación
    tienen el mismo alto de cabecera y el mismo color, y la interfaz se lee como
    una retícula en vez de como una lista de cajas parecidas.

    El cuerpo se pide con `body()` y no se expone el layout: quien añade un
    widget a una tarjeta no debería poder cambiarle los márgenes a la tarjeta.
    """

    def __init__(
        self,
        title: str | None = None,
        parent: QWidget | None = None,
        *,
        subtitle: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        if title is not None:
            header = QFrame()
            header.setObjectName("cardHeader")
            header_lay = QHBoxLayout(header)
            header_lay.setContentsMargins(14, 9, 14, 9)
            header_lay.setSpacing(8)
            label = QLabel(title)
            label.setObjectName("cardTitle")
            header_lay.addWidget(label)
            if subtitle is not None:
                sub = QLabel(subtitle)
                sub.setObjectName("hint")
                header_lay.addWidget(sub)
            header_lay.addStretch()
            #: Los widgets que cada página quiera poner a la derecha de la
            #: cabecera —un estado, un botón— se añaden por aquí.
            self.header = header_lay
            outer.addWidget(header)

        # El cuerpo vive dentro de **un widget propio**, y no colgado directamente
        # del layout exterior. No es un detalle de estilo: es lo que permite
        # plegar la tarjeta escondiendo una sola cosa.
        #
        # Esconder el contenido hijo a hijo —lo que se hacía antes— obligaba a
        # apuntar cuáles estaban visibles para devolverlos a su sitio al abrir,
        # porque hay contenido que se esconde solo: la tabla de rutas cuando no
        # hay rutas y el rótulo que la sustituye. Y esa contabilidad se rompía
        # por el otro lado: si el estado vacío cambiaba **con la tarjeta ya
        # plegada**, al abrir se restauraba la foto vieja y volvían a verse las
        # dos cosas a la vez. Con un contenedor, los hijos conservan su propia
        # visibilidad intacta y sólo se enseña o se esconde la caja que los
        # envuelve, así que no hay nada que recordar ni que pueda quedar
        # desincronizado.
        self._cuerpo = QWidget()
        self._body = QVBoxLayout(self._cuerpo)
        self._body.setContentsMargins(14, 12, 14, 12)
        self._body.setSpacing(10)
        outer.addWidget(self._cuerpo)

    def body(self) -> QVBoxLayout:
        """El layout donde va el contenido de la tarjeta."""
        return self._body

    def add_row(self, layout: QLayout) -> None:
        """Atajo para añadir una fila horizontal ya montada."""
        self._body.addLayout(layout)

    def set_collapsible(self, *, expanded: bool = False) -> QPushButton:
        """Pone un galón en la cabecera que muestra y esconde el cuerpo.

        Nace **plegada** por omisión, y ése es el cambio de fondo: una tarjeta que
        no se usa en cada visita ocupaba su alto entero las cien veces que no se
        usaba, y ese alto es justo el que empujaba a las demás fuera de la
        pantalla. Plegada sigue enseñando su título y su subtítulo —o sea, sigue
        diciendo qué hay dentro— y abre de un clic.

        Lo que se pliega es el widget que envuelve el cuerpo, no sus hijos: así el
        contenido conserva su propia visibilidad —una tabla vacía seguía vacía
        mientras estaba plegada— y abrir no tiene que reconstruir ningún estado.
        """
        if not hasattr(self, "header"):
            raise ValueError(
                "Sólo se puede plegar una tarjeta con cabecera: el galón va en ella. "
                "Esta se construyó sin título."
            )
        self._chevron = QPushButton("▸")
        self._chevron.setObjectName("chevron")
        self._chevron.setCheckable(True)
        self._chevron.setChecked(expanded)
        self._chevron.setFixedWidth(26)
        self._chevron.setToolTip("Mostrar u ocultar el contenido de esta tarjeta.")
        self._chevron.toggled.connect(self._set_body_visible)
        self.header.addWidget(self._chevron)
        self._set_body_visible(expanded)
        return self._chevron

    def set_expanded(self, expanded: bool) -> None:
        """Abre o cierra la tarjeta desde fuera, como haría el galón."""
        if not hasattr(self, "_chevron"):
            raise ValueError("Esta tarjeta no es plegable: llámese antes a set_collapsible.")
        self._chevron.setChecked(expanded)

    def _set_body_visible(self, visible: bool) -> None:
        if hasattr(self, "_chevron"):
            self._chevron.setText("▾" if visible else "▸")
        self._cuerpo.setVisible(visible)


class Chip(QLabel):
    """Etiqueta pequeña y redondeada: severidad, modo, estado de la ejecución.

    Se llama `Chip` y no `Badge` porque describe lo que es —una pastilla de
    estado— y no lo que parece. El color entra por parámetro y no se elige aquí:
    quién decide qué color significa qué es quien conoce el dato, no el widget.
    """

    def __init__(
        self,
        text: str = "",
        color: str = COLOR_MUTED,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(20)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background: {color}; color: white; border-radius: 10px;"
            " padding: 1px 10px; font-size: 11px; font-weight: 700;"
            " letter-spacing: 0.4px;"
        )

    def set_state(self, text: str, color: str) -> None:
        self.setText(text)
        self.set_color(color)


class Field(QWidget):
    """Una etiqueta y su control, con la etiqueta encima.

    En fila —«Cantidad: [ 1.0 ]»— la etiqueta compite con el valor por el mismo
    ancho y las columnas de una pantalla acaban desalineadas. Encima, el ojo
    recorre una sola columna de etiquetas y otra de valores, y los campos de
    sitios distintos se alinean solos.
    """

    def __init__(
        self,
        label: str,
        *,
        hint: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._label = QLabel(label)
        self._label.setObjectName("sectionTitle")
        lay.addWidget(self._label)
        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(8)
        lay.addLayout(self._row)
        if hint is not None:
            self._hint = QLabel(hint)
            self._hint.setObjectName("hint")
            self._hint.setWordWrap(True)
            lay.addWidget(self._hint)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._row.addWidget(widget, stretch)

    def add_stretch(self) -> None:
        self._row.addStretch()


class ConfirmationDialog(QDialog):
    """Diálogo que implementa `ConfirmationPrompt`.

    Muestra título, detalles y, si existe, el payload de la transacción sin firmar.
    """

    def __init__(
        self,
        action: PendingAction,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirmar acción — Amigocompora")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 16, 18, 16)

        title = QLabel(f"<b>{action.title}</b>")
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)
        title.setStyleSheet("font-size: 14px;")
        layout.addWidget(title)

        if action.details:
            details = QLabel("<br/>".join(f"• {d}" for d in action.details))
            details.setWordWrap(True)
            details.setStyleSheet(f"color: {COLOR_MUTED};")
            layout.addWidget(details)

        if action.transaction is not None:
            layout.addWidget(self._tx_widget(action.transaction))

        # El aviso va **encima** de los botones y separado por una línea: es lo
        # último que se lee antes de decidir, que es donde tiene que estar.
        layout.addWidget(divider())
        warning = QLabel()
        warning.setTextFormat(Qt.RichText)
        warning.setWordWrap(True)
        texto, color = warning_for(action.capability)
        warning.setText(texto)
        warning.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("Confirmar")
        buttons.button(QDialogButtonBox.No).setText("Cancelar")
        # El «sí» lleva el mismo color que el aviso: en el diálogo que firma, el
        # botón que confirma es el peligroso y tiene que parecerlo.
        buttons.button(QDialogButtonBox.Yes).setObjectName(
            "danger" if action.is_irreversible else "secondary"
        )
        buttons.button(QDialogButtonBox.No).setObjectName("secondary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _tx_widget(tx: PlannedTransaction) -> QWidget:
        box = QFrame()
        box.setStyleSheet(
            f"background: {COLOR_BG}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: 6px;"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(5)
        # La vista no sabe de qué red es el payload: cada tipo se describe a sí
        # mismo y aquí sólo se pinta. Añadir una red nueva no toca este diálogo.
        for label, value in tx.describe():
            row = QLabel(
                f"<span style='color:{COLOR_MUTED}'>{label}:</span> "
                f"<span style='color:{COLOR_TEXT}'>{_clip(value)}</span>"
            )
            row.setTextFormat(Qt.RichText)
            row.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.setWordWrap(True)
            lay.addWidget(row)
        return box


class QtConfirmationPrompt:
    """Adaptador `ConfirmationPrompt` que abre `ConfirmationDialog` en el hilo de Qt.

    Se instala en `ConfirmationGateway` desde `MainWindow`.
    """

    def __init__(self, parent: QWidget) -> None:
        self._parent = parent

    async def ask(self, action: PendingAction) -> bool:
        # qasync hace que este `await` ceda al event loop de Qt sin bloquear.
        dialog = ConfirmationDialog(action, self._parent)
        result = dialog.exec()
        return result == QDialog.Accepted


class AlertBanner(QWidget):
    """Banner superior que muestra la última alerta no reconocida."""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        self._dot = QLabel("●")
        lay.addWidget(self._dot)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        lay.addWidget(self._label, stretch=1)
        btn = QPushButton("Descartar")
        btn.setObjectName("secondary")
        btn.clicked.connect(self._on_dismiss)
        lay.addWidget(btn)
        self.setStyleSheet(
            f"AlertBanner {{ background: {COLOR_ELEVATED};"
            f" border: 1px solid {COLOR_BORDER_STRONG}; border-radius: 6px; }}"
        )

    def show_alert(self, title: str, detail: str, severity: str) -> None:
        color = {"low": COLOR_MUTED, "medium": COLOR_WARNING, "high": COLOR_DANGER}.get(
            severity, COLOR_MUTED
        )
        self._dot.setStyleSheet(f"color: {color}; font-weight: 700;")
        self._label.setText(
            f"<span style='color:{color}; font-weight:700'>{severity.upper()}</span>"
            f"  <b>{title}</b> — {detail}"
        )
        self._label.setTextFormat(Qt.RichText)
        self.setVisible(True)

    def _on_dismiss(self) -> None:
        self.setVisible(False)
        self.dismissed.emit()


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    # Mostrar en hora local del usuario.
    try:
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt.isoformat()
