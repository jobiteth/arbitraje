"""Las rutas de un swap: tarjetas para comparar y un panel para leer la elegida.

### Por qué dos piezas y no una tabla

La tabla de ocho columnas cabe, pero no se lee: cada fila mezcla siete cifras de
anchos distintos y el ojo no sabe cuál comparar. Aquí se separan las dos
preguntas que el usuario hace de verdad:

1. **¿Cuál elijo?** Se contesta comparando, y para comparar sirve una lista de
   tarjetas de dos líneas: el venue y lo que se recibe en grande, y debajo el
   desglose —comisión, impacto, liquidez y, si cruza varios pools, el camino—.
   La mejor va en verde, y lo que no se puede firmar lleva su marca ámbar.
2. **¿Qué estoy a punto de firmar?** Se contesta de una en una, y para eso está
   el panel de detalle: etiqueta y valor, como en cualquier pantalla de swap,
   sin cifras de otras rutas compitiendo al lado.

### Lo que el panel añade, y por qué

Hay datos que existían y no se enseñaban en ninguna parte: el **camino** de la
ruta cuando cruza varios pools (`Quote.route`), la **cartera receptora**, el
**deslizamiento máximo** configurado y el **coste de red**. Los tres primeros se
leen de la cotización y la configuración; el coste de red **no se inventa aquí**:
llega estimado por el caso de uso cuando se prepara el swap —es una llamada a la
red por ruta y no se hace por cada comparación— y esta vista sólo lo pinta, con
sus tres desenlaces posibles y ninguno de ellos disfrazado de cero.

### El resaltado de la tarjeta elegida

Con `setItemWidget` la tarjeta queda **encima** del resaltado que pinta la lista
para el item seleccionado, así que `::item:selected` no se vería. Por eso el
estado —elegida, con el ratón encima— viaja como propiedad dinámica a la tarjeta
y lo pinta la hoja de estilos sobre su propio marco. Es la única forma de que la
selección se vea sin dibujar la tarjeta dos veces.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QEnterEvent, QFontMetrics, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.usecases.estimate_cost import NetworkCost
from amigocompora.domain.addresses import shorten
from amigocompora.domain.models import Quote
from amigocompora.ui import icons
from amigocompora.ui.theme import COLOR_MUTED, COLOR_SUCCESS, COLOR_WARNING, ICON_PX

#: Alto de una tarjeta de ruta. Fijo a propósito: la lista alinea sus filas —y no
#: las mide una a una— y así veinte rutas no bailan de alto según lo larga que
#: salga una nota. El texto que no cabe se recupera por el tooltip.
_ROW_HEIGHT = 62

#: Margen horizontal de la tarjeta. Es también lo que se descuenta al recortar la
#: segunda línea, así que vive en una constante y no en dos números que se
#: separarían al primer ajuste.
_MARGIN_X = 12


def _short_amount(texto: str, decimales: int = 4) -> str:
    """Un importe como `«0.0265213038 UNI»` con `decimales` decimales.

    Se **trunca**, no se redondea: `0,026521…` se lee como `0,0265`, y nunca como
    algo mayor de lo que hay. Un valor distinto de cero que se volvería cero (un
    `0,00002` a 4 decimales) se muestra con sus dos primeras cifras significativas,
    porque un `0,0000` debajo de una cifra real también miente. Si el texto no es un
    número, se devuelve tal cual.
    """
    numero, _, simbolo = texto.partition(" ")
    try:
        valor = Decimal(numero)
    except InvalidOperation:
        return texto
    if valor == 0:
        corto = Decimal(0).quantize(Decimal(1).scaleb(-decimales))
    else:
        corto = valor.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_DOWN)
        if corto == 0:
            # `adjusted()` es el exponente de la cifra más significativa: dos cifras
            # significativas llegan hasta `adjusted() - 1`.
            corto = valor.quantize(
                Decimal(1).scaleb(valor.adjusted() - 1), rounding=ROUND_DOWN
            )
    return f"{corto:f} {simbolo}" if simbolo else f"{corto:f}"


def _repolish(widget: QWidget) -> None:
    """Vuelve a aplicar la hoja de estilos tras cambiar una propiedad dinámica.

    Qt no repinta solo cuando cambia una propiedad: hay que pedirle que olvide el
    estilo y lo recalcule. Sin esto la propiedad cambia y el color no.
    """
    estilo = widget.style()
    estilo.unpolish(widget)
    estilo.polish(widget)


class RouteCard(QFrame):
    """Una ruta en dos líneas: lo que se recibe, y el desglose.

    La primera línea es lo que se compara —icono del motor, venue, lo que se
    recibe— y la segunda el porqué de esa cifra. El importe va en verde sólo si
    es la mejor de la comparación, y la marca ámbar «· sólo cotiza» aparece
    **antes** de elegirla, que es cuando sirve de algo.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("routeCard")
        #: La cotización que esta tarjeta pinta, para quien la lea desde fuera.
        self.quote: Quote | None = None
        self._amount_full = ""
        self._line_full = ""
        self._completo = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(_MARGIN_X, 8, _MARGIN_X, 8)
        outer.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.brand = QLabel()
        self.brand.setFixedSize(ICON_PX, ICON_PX)
        top.addWidget(self.brand)
        self.venue = QLabel("")
        self.venue.setObjectName("routeName")
        top.addWidget(self.venue)
        #: La marca de «sólo cotiza». Se enseña u oculta; el texto no cambia.
        self.mark = QLabel("· sólo cotiza")
        self.mark.setObjectName("routeMark")
        top.addWidget(self.mark)
        top.addStretch(1)
        self.amount = QLabel("")
        self.amount.setObjectName("routeAmount")
        top.addWidget(self.amount)
        outer.addLayout(top)

        #: La segunda línea. Se recorta con puntos suspensivos cuando no cabe —una
        #: fila por ruta, para poder comparar de un vistazo— y el texto entero
        #: queda en su tooltip.
        self.line = QLabel("")
        self.line.setObjectName("routeLine")
        self.line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.line)

    def set_quote(self, quote: Quote, *, es_mejor: bool, firmable: bool) -> None:
        """Pinta una ruta.

        `es_mejor` es «la primera de la comparación» y `firmable` distingue la
        ruta que algún motor activo sabe construir de la que sólo se cotiza: son
        dos cosas distintas del mismo dato y no se pueden confundir —una ruta
        puede ser la mejor y aun así no firmarse—.
        """
        self.quote = quote
        self.brand.setPixmap(icons.brand_icon(quote.engine_id).pixmap(ICON_PX, ICON_PX))
        self.venue.setText(quote.venue.name)
        self.venue.setToolTip(quote.venue.name)
        self.mark.setVisible(not firmable)
        if not firmable:
            self.mark.setToolTip(
                "Esta ruta no se puede firmar: el motor que la observó sólo "
                "cotiza, y el swap lo construye otro (uniswap, 0x…)."
            )
        self._amount_full = str(quote.amount_out)
        # El verde de «la mejor» va aquí y no en la hoja porque sólo la primera
        # ruta lo lleva: una regla qss no puede contar filas.
        self.amount.setStyleSheet(f"color: {COLOR_SUCCESS};" if es_mejor else "")
        self._line_full = self._resumen_de(quote)
        tooltip = quote.source_note
        if not firmable:
            tooltip = (
                f"{tooltip}\nEsta ruta no se puede firmar: el motor que la observó "
                f"sólo cotiza, y el swap lo construye otro (uniswap, 0x…). Elige "
                f"una ruta de un motor que construya."
            ).strip()
        self.setToolTip(tooltip)
        self._render()

    def set_completo(self, completo: bool) -> None:
        """Enseña el importe entero o recortado, según la casilla de la tarjeta."""
        self._completo = completo
        self._render()

    def set_selected(self, selected: bool) -> None:
        """Marca la tarjeta como la elegida. Lo pinta la hoja de estilos."""
        self._set_state("selected", selected)

    def _render(self) -> None:
        texto = self._amount_full if self._completo else _short_amount(self._amount_full)
        self.amount.setText(texto)
        self.amount.setToolTip(self._amount_full)
        self._elide()

    def _elide(self) -> None:
        """Recorta la segunda línea al ancho que hay, con puntos suspensivos."""
        ancho = self.width() - 2 * _MARGIN_X
        if ancho <= 0:
            # Todavía no hay geometría —el widget no se ha mostrado—: se deja el
            # texto entero, que es lo que una prueba puede leer y afirmar.
            self.line.setText(self._line_full)
            return
        metrics = QFontMetrics(self.line.font())
        self.line.setText(metrics.elidedText(self._line_full, Qt.TextElideMode.ElideRight, ancho))

    @staticmethod
    def _resumen_de(quote: Quote) -> str:
        """La segunda línea: comisión, impacto, liquidez y, si las hay, las patas.

        El camino sólo se escribe cuando la ruta **cruza varios pools**: en un
        solo pool es el par que ya está sobre la tarjeta, y repetirlo gastaría la
        línea en decir dos veces lo mismo.
        """
        comision = (
            f"{quote.fee_bps.value} bps" if quote.fee_bps is not None else "no desglosada"
        )
        impacto = (
            f"{quote.price_impact_bps.value} bps"
            if quote.price_impact_bps is not None
            else "no publicado"
        )
        liquidez = str(quote.liquidity) if quote.liquidity else "—"
        partes = [f"Comisión {comision}", f"Impacto {impacto}", f"Liquidez {liquidez}"]
        if quote.route is not None:
            partes.append(" → ".join(token.symbol for token in quote.route.tokens))
        return " · ".join(partes)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._set_state("hover", True)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._set_state("hover", False)

    def _set_state(self, name: str, value: bool) -> None:
        if self.property(name) == value:
            return
        self.setProperty(name, value)
        _repolish(self)


class RouteList(QListWidget):
    """La lista de rutas: un renglón por cotización, con su tarjeta dentro.

    Se usa un `QListWidget` y no un `QVBoxLayout` de tarjetas porque lo que se
    quiere es un listbox con su oficio: teclado, rueda, un solo elemento elegido
    y una señal —`currentRowChanged`— que la página ya sabe escuchar.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("routeList")
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Sin barra horizontal: si algo no cabe, se recorta con puntos en la
        # segunda línea. Una barra horizontal aquí sería la señal de que el
        # reparto a lo ancho no está funcionando.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setSpacing(6)
        self._cards: list[RouteCard] = []
        # El resaltado lo pinta cada tarjeta —ver el docstring del módulo—, así
        # que la lista se encarga de decirle cuál está elegida.
        self.currentRowChanged.connect(self._paint_selection)

    def set_quotes(self, quotes: Sequence[Quote], *, firmables: Sequence[bool]) -> None:
        """Rearma la lista con las rutas que se van a enseñar.

        `firmables` viaja en paralelo a `quotes` —y no se pregunta aquí— porque
        quién puede construir cada swap lo sabe el registro de motores, no la
        vista. La primera ruta es la mejor: la comparación ya llega ordenada.
        """
        self._cards = []
        self.clear()
        for indice, (quote, firmable) in enumerate(zip(quotes, firmables, strict=True)):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, quote)
            item.setSizeHint(QSize(0, _ROW_HEIGHT))
            self.addItem(item)
            card = RouteCard()
            card.set_quote(quote, es_mejor=indice == 0, firmable=firmable)
            self.setItemWidget(item, card)
            self._cards.append(card)
        self._paint_selection(self.currentRow())

    def select_row(self, row: int) -> None:
        """Elige una fila como lo haría un clic. -1 significa «ninguna»."""
        self.setCurrentRow(row)

    def current_row(self) -> int:
        """La fila elegida, o -1 si no hay ninguna."""
        return self.currentRow()

    def card(self, row: int) -> RouteCard | None:
        """La tarjeta de una fila, para quien tenga que pintarla desde fuera."""
        if not 0 <= row < len(self._cards):
            return None
        return self._cards[row]

    def cards(self) -> tuple[RouteCard, ...]:
        """Todas las tarjetas, en el orden de la lista."""
        return tuple(self._cards)

    def cap_height(self, *, filas: int = 10) -> None:
        """Le pone techo al alto: el de sus tarjetas, no el que le sobre.

        Un listbox dentro de una tarjeta que estira ocuparía todo el alto que le
        den aunque tenga dos rutas dentro. Con el techo puesto, la tarjeta mide lo
        que mide su contenido, y a partir del tope de filas la lista se desplaza,
        que es lo que sabe hacer.
        """
        visible = min(self.count(), filas)
        alto = (
            2 * self.frameWidth()
            + visible * _ROW_HEIGHT
            + max(0, visible - 1) * self.spacing()
        )
        self.setMaximumHeight(alto if visible else 0)

    def _paint_selection(self, row: int) -> None:
        for indice, card in enumerate(self._cards):
            card.set_selected(indice == row)


#: Las filas del panel, en orden. La clave es la que usan la página y las pruebas
#: para pedir un valor; el rótulo es el que se lee.
_ROWS: tuple[tuple[str, str], ...] = (
    ("recibes", "Recibes (incl. comisión)"),
    ("ruta", "Ruta"),
    ("motor", "Motor"),
    ("precio", "Precio"),
    ("comision", "Comisión"),
    ("impacto", "Impacto"),
    ("liquidez", "Liquidez"),
    ("deslizamiento", "Deslizamiento máx."),
    ("cartera", "Cartera receptora"),
    ("coste", "Coste de red"),
    ("origen", "Origen"),
)

#: Lo que dice el coste de red antes de preparar. Se escribe aquí y no en la
#: página para que el estado inicial y el que queda al cambiar de ruta digan
#: exactamente lo mismo.
_SIN_ESTIMAR = "se estima al preparar"


class RouteDetail(QWidget):
    """La ruta elegida, en filas de etiqueta y valor.

    Es el panel que contesta «¿qué estoy a punto de firmar?»: los datos de una
    sola ruta, sin cifras de otras compitiendo al lado. Cuando no hay ninguna
    elegida lo dice, en vez de quedarse en blanco.

    El coste de red es la única fila que no sale de la cotización: llega
    estimado al preparar el swap y aquí sólo se pinta, con sus cuatro estados
    —«se estima al preparar», la cifra, «no se pudo estimar» y «—» para lo que
    no aplica—, ninguno de ellos disfrazado de cero.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("routeDetail")
        self._quote: Quote | None = None
        self._amount_full = ""
        self._precio_full = ""
        self._completo = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self.placeholder = QLabel("Elige una ruta de la lista para ver su detalle.")
        self.placeholder.setObjectName("empty")
        self.placeholder.setWordWrap(True)
        outer.addWidget(self.placeholder)

        self._grid_widget = QWidget()
        grid = QGridLayout(self._grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)

        #: Los valores por clave de fila (`_ROWS`). Es público porque la página y
        #: las pruebas leen de aquí; los rótulos no se exponen, no se cambian.
        self.values: dict[str, QLabel] = {}
        for row, (key, etiqueta) in enumerate(_ROWS):
            name = QLabel(etiqueta)
            name.setObjectName("detailLabel")
            valor = QLabel("")
            valor.setObjectName("detailAmount" if key == "recibes" else "detailValue")
            valor.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            valor.setWordWrap(True)
            valor.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(
                name, row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            grid.addWidget(valor, row, 1)
            self.values[key] = valor
        outer.addWidget(self._grid_widget)
        self.clear()

    def set_quote(
        self,
        quote: Quote,
        *,
        firmable: bool,
        owner: str,
        slippage_bps: int,
    ) -> None:
        """Pinta el detalle de una ruta.

        `owner` es la cartera que firmaría —la receptora del swap en el flujo
        normal— y `slippage_bps` la tolerancia configurada. Los dos llegan desde
        fuera y no se leen aquí: el panel enseña, no decide.
        """
        self._quote = quote
        self._grid_widget.setVisible(True)
        self.placeholder.setVisible(False)

        self._amount_full = str(quote.amount_out)
        self._precio_full = str(quote.price)
        self._render_cifras()

        self._set("ruta", f"{quote.venue.name} · {_camino(quote)}", tooltip=_tramos(quote))
        self._set(
            "motor",
            f"{quote.engine_id} · sólo cotiza" if not firmable else quote.engine_id,
            tooltip=(
                "Esta ruta no se puede firmar: el motor que la observó sólo cotiza, "
                "y el swap lo construye otro (uniswap, 0x…)."
                if not firmable
                else ""
            ),
            color=None if firmable else COLOR_WARNING,
        )

        if quote.fee_bps is None:
            self._set(
                "comision",
                "no desglosada por la fuente (ya descontada de lo que recibes)",
                alerta=True,
            )
        else:
            self._set(
                "comision",
                (
                    f"{quote.fee_bps} [{quote.fee_basis.value}]"
                    if quote.fee_basis
                    else str(quote.fee_bps)
                ),
                tooltip=(
                    f"Procedencia: {quote.fee_basis.label}." if quote.fee_basis else ""
                ),
            )
        if quote.price_impact_bps is None:
            self._set(
                "impacto",
                "no publicado por la fuente (ya está dentro de lo que recibes)",
                alerta=True,
            )
        else:
            self._set(
                "impacto",
                f"{quote.price_impact_bps} [{quote.impact_basis.value}]"
                if quote.impact_basis
                else str(quote.price_impact_bps),
                tooltip=(
                    f"Procedencia: {quote.impact_basis.label}."
                    if quote.impact_basis
                    else ""
                ),
                alerta=not quote.is_exact,
            )

        self._set("liquidez", str(quote.liquidity) if quote.liquidity else "—")
        self._set(
            "deslizamiento",
            f"{slippage_bps} bps",
            tooltip=(
                "Tolerancia con la que se construye el swap: el mínimo que aceptas "
                "recibir, fijado con el engranaje de la tarjeta de intercambio y "
                "guardado en config.toml. Cuanto más baja, más te protege de un "
                "movimiento del precio y más fácil es que la operación revierta."
            ),
        )
        if owner:
            self._set("cartera", shorten(owner), tooltip=owner)
        else:
            self._set("cartera", "sin cartera configurada", color=COLOR_MUTED)
        self._set("origen", quote.source_note or "—", tooltip=quote.source_note)

    def set_completo(self, completo: bool) -> None:
        """Enseña las cifras enteras o recortadas, según la casilla de la tarjeta."""
        self._completo = completo
        self._render_cifras()

    def clear(self) -> None:
        """Vuelve al estado «ninguna ruta elegida», que dice qué hacer."""
        self._quote = None
        self._amount_full = ""
        self._precio_full = ""
        self._grid_widget.setVisible(False)
        self.placeholder.setVisible(True)
        for key, label in self.values.items():
            if key == "coste":
                continue
            label.setText("")
            label.setToolTip("")
            label.setStyleSheet("")
        self.reset_cost()

    def reset_cost(self) -> None:
        """Devuelve la fila del coste a «se estima al preparar».

        Se llama al cambiar de ruta: la estimación era de la ruta anterior y
        dejarla escrita al lado de otra sería enseñar el coste de otro swap.
        """
        self._set(
            "coste",
            _SIN_ESTIMAR,
            tooltip=(
                "El coste de red se estima al preparar el swap: es una llamada a "
                "la red por ruta y no se hace al comparar precios."
            ),
            color=COLOR_MUTED,
        )

    def set_cost(self, coste: NetworkCost | None, error: str | None) -> None:
        """Pinta el desenlace de la estimación: la cifra, el motivo o «—».

        Los tres estados son distintos y ninguno es «cero»: hay un coste con sus
        cifras, hay un intento fallido con su motivo, y hay un «no aplica» —una
        red sin `eth_estimateGas`— que se escribe con raya.
        """
        if coste is not None:
            self._set(
                "coste",
                f"≈ {coste.native}",
                tooltip=(
                    f"gas {coste.gas_limit} por {coste.gas_price_gwei} gwei — "
                    f"coste máximo; {coste.source}. El gas que sobre del límite no "
                    f"se paga y la tarifa puede moverse antes de emitir. No incluye "
                    f"la aprobación del token, que sería otra transacción."
                ),
            )
        elif error is not None:
            self._set(
                "coste",
                "no se pudo estimar",
                tooltip=(
                    f"{error}\nLa preparación sigue igual: el borrador existe sin "
                    f"el dato, y el gas se recalcula al emitir."
                ),
                color=COLOR_WARNING,
            )
        else:
            self._set(
                "coste",
                "—",
                tooltip=(
                    "Esta red no se estima con `eth_estimateGas` (Solana), así que "
                    "no hay coste de red de EVM que enseñar."
                ),
                color=COLOR_MUTED,
            )

    def _render_cifras(self) -> None:
        """Recorta —o no— las dos cifras que la casilla de decimales gobierna.

        Son el importe recibido y el precio de ejecución: las mismas dos que
        recortaba la tabla. El valor completo va en el tooltip de cada una, que
        es el otro modo de leerla.
        """
        if self._completo:
            self._set("recibes", self._amount_full, tooltip=self._amount_full)
            self._set("precio", self._precio_full, tooltip=self._precio_full)
            return
        self._set("recibes", _short_amount(self._amount_full), tooltip=self._amount_full)
        self._set("precio", _short_amount(self._precio_full), tooltip=self._precio_full)

    def _set(
        self,
        key: str,
        texto: str,
        *,
        tooltip: str = "",
        color: str | None = None,
        alerta: bool = False,
    ) -> None:
        label = self.values[key]
        label.setText(texto)
        label.setToolTip(tooltip)
        elegido = COLOR_WARNING if alerta else color
        label.setStyleSheet(f"color: {elegido};" if elegido else "")


def _camino(quote: Quote) -> str:
    """Las patas de la ruta, en orden: `WETH → USDC`, o el par si es un solo pool."""
    if quote.route is not None:
        return " → ".join(token.symbol for token in quote.route.tokens)
    return f"{quote.pair.base.symbol} → {quote.pair.quote.symbol}"


def _tramos(quote: Quote) -> str:
    """El tooltip de la ruta: los tramos y la comisión de cada uno, si la llevan.

    Es donde se puede leer lo que en una línea no cabe: por qué pools pasa el
    swap cuando cruza varios, y con qué comisión cada tramo.
    """
    if quote.route is None:
        return (
            "Un solo pool: el swap va directo de "
            f"{quote.pair.base.symbol} a {quote.pair.quote.symbol}."
        )
    tramos = "; ".join(
        f"{hop.base.symbol} → {hop.quote.symbol} "
        f"({hop.fee_bps if hop.fee_bps is not None else 'comisión no publicada'})"
        for hop in quote.route.hops
    )
    return f"{len(quote.route.hops)} tramos: {tramos}."
