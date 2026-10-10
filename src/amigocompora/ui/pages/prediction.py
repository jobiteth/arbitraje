"""Pestaña: Mercados de predicción — la lista, y la tarjeta de cada mercado.

Dos vistas en la misma pestaña, y un `QStackedWidget` que cambia de una a otra.
La primera es **solo la lista**: buscar, acotar por cierre y elegir un mercado.
La segunda es la **tarjeta de ese mercado**, que se abre al pulsar su fila y
vuelve a la lista con «← Volver a la lista». La pestaña dejó de enseñarlo todo a
la vez —tabla, tarjeta de orden, wallet, cestas y cobro apilados— porque elegir y
operar son dos momentos distintos: la lista se lee de un vistazo, y la tarjeta se
trabaja con el ancho entero de la ventana.

### La tarjeta del mercado

Es la antigua tarjeta de orden con lo que le faltaba para parecerse a la de
Polymarket: el resultado se elige con botones, el lado con dos pestañas
(Comprar / Vender) y el tamaño con **atajos fijos y editables** que siguen la
convención del recinto —al comprar se elige **importe** ($1/$5/$20/$100) y las
participaciones se derivan hacia abajo, porque lo que se firma sigue siendo un
tamaño; al vender se eligen participaciones (5/10/25/50/100)—. El precio sigue
siendo el de una orden límite, con su propuesta desde el libro real.

Dos hechos mandan en el diseño, como antes:

1. **El libro es un hecho y el precio publicado es una opinión.** La probabilidad
   implícita de un resultado es el último precio cruzado; lo que costaría cruzar
   ahora un tamaño dado es otra cosa. La tarjeta pide el libro del resultado
   elegido y enseña las dos cifras, porque la diferencia entre ellas es
   exactamente el margen de la operación.
2. **Publicar no es emitir.** Una orden firmada se puede cancelar mientras nadie
   la haya cruzado; una transacción emitida no. Todo lo que dice la tarjeta está
   redactado para que esa diferencia no se pierda.

### El saldo, en las dos caras

En la cara de compra y en la de venta hay un texto con el **saldo de la wallet de
depósito** y un botón al lado que abre su **panel**: la dirección con copiar y
QR, el saldo, los dos caminos para añadirle fondos y la tabla de posiciones con
«Vender» y «Comprar más». Polymarket ya no opera desde una cartera normal: las
compras y las ventas van por esa **deposit wallet**, un contrato que custodia el
colateral y las participaciones y que controla la misma clave. Cargar una
posición **no firma** —deja el mercado, el resultado, el lado y el tamaño
puestos— y revisar y publicar sigue siendo el botón de la tarjeta. La lectura del
saldo es pública y se hace una vez, al abrir la primera tarjeta; el panel tiene
su propio botón para releerla. Lo que todavía no se puede es cobrar sus
posiciones resueltas —eso pide un lote del relayer que la aplicación aún no
construye— y la fila lo dice apagando sus botones con el motivo, en vez de
ofrecer algo que no existe.

### Y lo que no cabe en la tarjeta

«Cestas con margen» y «Por cobrar» viven en **diálogos** que abren sus botones
junto a «Buscar». Son consultas de conjunto —todas las cestas de todos los
mercados, todo lo resoluble de la cartera— y no de un mercado concreto, así que
no tienen sitio en una tarjeta de mercado ni motivo para ocupar la lista.

La tarjeta de cobro cierra el ciclo: lo que se compró y salió a favor hay que
**cobrarlo**, y hasta que no se cobra el colateral sigue dentro del contrato. Es
la única operación de la aplicación por la que el dinero **entra**, y de ahí sale
lo que la distingue: no le aplican los topes de gasto —acotan lo que sale— y no
pide ningún permiso previo, porque el contrato quema las participaciones de quien
firma. Lo que comparte con las demás es la regla del botón: apagado **con el
motivo escrito**.

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

### Categorías, orden y tendencia

La lista se ordena por **más nuevas** por omisión, y con ella vinieron dos
selectores en la barra de búsqueda. «Categoría» ofrece **Tendencia** —la vista de
lo que más se mueve en 24 h, que no filtra sino que ordena, y por eso su elección
apaga el selector de orden—, **Todas**, y las etiquetas del evento **tal cual las
publica Polymarket**, ordenadas por número de mercados. Las etiquetas no viajan
en el mercado sino en su evento, así que el motor las pide aparte y una consulta
fallida deja la lista **sin** etiquetas en vez de sin mercados. «Orden» elige
entre más nuevas, más volumen y lo incoherente.

Los dos selectores **recortan en cliente** lo ya traído, como el de ventana, y es
la siguiente búsqueda la que los pide a la fuente para que el límite se gaste en
lo que se quiere ver. La tabla enseña además la columna «24 h» —el cambio del
precio por participación, en céntimos, verde si subió y rojo si bajó— y la
tarjeta del mercado, la tendencia de 1 h / 24 h / 1 semana, el volumen, la
liquidez, el diferencial y las etiquetas, con «cuánto paga» una participación al
precio límite escrito («×1.61 · +61.3 %»).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amigocompora.app.container import Container
from amigocompora.app.usecases.analyze_prediction_market import (
    MarketReport,
    filter_reports_by_tag,
    sort_reports,
)
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
from amigocompora.domain.execution import ExecutionLimits
from amigocompora.domain.models import (
    MarketDepth,
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionPosition,
    PredictionSide,
    PredictionSort,
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
    AmountSpinBox,
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
    # Los dos cortos existen para lo que cierra ya —las subidas/bajadas de
    # cinco minutos, por ejemplo—: se piden a la fuente por fecha de cierre,
    # así que el límite no se gasta en lo que cierra dentro de un mes.
    ("5 minutos", timedelta(minutes=5)),
    ("10 minutos", timedelta(minutes=10)),
    ("24 h", timedelta(hours=24)),
    ("7 días", timedelta(days=7)),
    ("30 días", timedelta(days=30)),
)

#: Los órdenes que ofrece el selector, con «más nuevas» de primero: la lista por
#: omisión es la de lo recién creado. El dato que se guarda en el desplegable es
#: el `.value` del enum, porque Qt convierte los enums a texto al guardarlos.
_SORTS: tuple[tuple[str, PredictionSort], ...] = (
    ("más nuevas", PredictionSort.NEWEST),
    ("más volumen", PredictionSort.VOLUME),
    ("lo incoherente", PredictionSort.COHERENCE),
)

#: El centinela de «Tendencia» en el selector de categoría. No es una etiqueta
#: —no hay `tag_id` que pedirle a la fuente—: es la vista de lo que más se mueve
#: en 24 h, y por eso viaja como un objeto propio y no como un `MarketTag`.
_TRENDING = object()

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

#: Los atajos de tamaño de la tarjeta. Al comprar se elige **importe** y al
#: vender **participaciones**, siguiendo la convención del recinto: la orden que
#: se firma lleva un tamaño, y en la compra solo puede derivarse (hacia abajo) de
#: un importe. El importe por omisión da al menos el mínimo del recinto a
#: cualquier precio por debajo de 1 —5 / 0,99 sigue pasando de 5
#: participaciones—, así que una tarjeta recién abierta nunca nace bloqueada por
#: tamaño.
_BUY_AMOUNTS: tuple[Decimal, ...] = (
    Decimal(1),
    Decimal(5),
    Decimal(20),
    Decimal(100),
)
_SELL_SHARES: tuple[int, ...] = (5, 10, 25, 50, 100)
_DEFAULT_BUY_AMOUNT = Decimal(5)

#: El importe mínimo del recinto para una **compra que cruza el libro**. Medido:
#: «invalid amount for a marketable BUY order (…), min size: $1». Las órdenes
#: que descansan se validan por tamaño —`min_order_size` del mercado—, y las
#: ventas también.
_MIN_MARKETABLE_BUY_AMOUNT = Decimal(1)


def _cents(change: Decimal | None) -> str:
    """Un cambio de precio en céntimos: «+1 ¢», «−23 ¢», «−2.65 ¢» o «—».

    Medido el 2026-10-09: la fuente publica el cambio en **unidades de precio**
    —un mercado cuyo «sí» pasó de 0.445 a 0.235 trae −0.21, exactamente el
    delta—, así que multiplicar por 100 lo deja en la unidad con la que se lee
    un precio de Polymarket: el céntimo. El guion es para un dato que la fuente
    no publicó; un cero sería afirmar que no se movió.
    """
    if change is None:
        return "—"
    if change == 0:
        return "0 ¢"
    texto = f"{abs(change) * 100:.2f}".rstrip("0").rstrip(".")
    if texto == "0":
        # Un movimiento menor de medio céntimo sigue siendo un movimiento, pero
        # «−0 ¢» se leería como un error de la aplicación.
        texto = "0.01"
    signo = "+" if change > 0 else "−"
    return f"{signo}{texto} ¢"


def _spread_text(spread: Decimal | None) -> str:
    """El diferencial en céntimos, sin signo: «1 ¢», «0.1 ¢» o «—»."""
    if spread is None:
        return "—"
    return f"{(spread * 100).normalize():f} ¢"


def _trend_html(market: PredictionMarket) -> str:
    """La línea de tendencia: «1 h · 24 h · 1 sem», con color y guion si falta.

    Cada tramo va en verde si el precio subió y en rojo si bajó: lo que se lee
    de un vistazo es la dirección, y el signo queda para quien mire la cifra.
    """
    tramos = (
        ("1 h", market.price_change_1h),
        ("24 h", market.price_change_24h),
        ("1 sem", market.price_change_1w),
    )
    partes: list[str] = []
    for etiqueta, cambio in tramos:
        if cambio is None:
            partes.append(f"{etiqueta} <span style='color:{COLOR_MUTED}'>—</span>")
            continue
        if cambio > 0:
            color = COLOR_SUCCESS
        elif cambio < 0:
            color = COLOR_DANGER
        else:
            color = COLOR_MUTED
        partes.append(f"{etiqueta} <span style='color:{color}'>{_cents(cambio)}</span>")
    return "Tendencia: " + " · ".join(partes)


def _stats_text(market: PredictionMarket) -> str:
    """Volumen de 24 h, liquidez y diferencial, sin inventar ninguna cifra."""
    return (
        f"Volumen 24 h: {format_amount(market.volume_24h) if market.volume_24h is not None else '—'}"
        f" · Liquidez: {format_amount(market.liquidity) if market.liquidity is not None else '—'}"
        f" · Diferencial: {_spread_text(market.spread)}"
    )


def _tags_text(market: PredictionMarket) -> str:
    """Las etiquetas del evento tal cual las publica la fuente, o un guion."""
    if not market.tags:
        return "Etiquetas: —"
    return "Etiquetas: " + " · ".join(tag.label for tag in market.tags)


def _criteria_text(
    window: timedelta | None, sort: PredictionSort, category: MarketTag | None
) -> str:
    """El sufijo del estado: con qué criterios se ha pedido la lista.

    Nombra el orden **efectivo** y no el del selector: con «Tendencia» elegida
    el selector está apagado y manda su orden, y decir «lo incoherente» ahí sería
    describir una pantalla que no es la que se está viendo.
    """
    partes: list[str] = []
    if window is not None:
        partes.append("lo que antes cierra, primero")
    elif sort is PredictionSort.NEWEST:
        partes.append("las más nuevas primero")
    elif sort is PredictionSort.TRENDING:
        partes.append("lo que más se mueve, primero")
    elif sort is PredictionSort.VOLUME:
        partes.append("más volumen primero")
    else:
        partes.append("lo incoherente primero")
    if category is not None:
        partes.append(f"categoría: {category.label}")
    return " · " + " · ".join(partes)


def _category_index(combo: QComboBox, actual: object) -> int:
    """El índice del ítem que corresponde a la selección anterior, o 0.

    Se compara por identidad y, entre etiquetas, por `slug`; nunca por el texto
    del ítem, que lleva el número de mercados y cambia con cada búsqueda. No hay
    caso «no está»: el ítem de la etiqueta elegida se reinserta antes de llegar
    aquí, precisamente para que la selección del usuario no se mueva sola.
    """
    for index in range(combo.count()):
        dato = combo.itemData(index)
        if dato is actual:
            return index
        if (
            isinstance(dato, MarketTag)
            and isinstance(actual, MarketTag)
            and dato.slug == actual.slug
        ):
            return index
    return 0


def _payout_text(price: float) -> str:
    """Cuánto paga una participación al precio escrito: «×1.61 · +61.3 %».

    Es la otra cara del precio: si cada participación cuesta `p` y paga 1 cuando
    acierta, el multiplicador es `1/p` y la ganancia sobre lo pagado,
    `(1/p − 1)·100 %`. Se calcula con el precio que se va a firmar, así que se
    recalcula al cambiar de resultado o de precio; con un precio no positivo
    —el campo no lo permite, pero el cálculo no puede suponerlo— no dice nada en
    vez de dividir por cero.
    """
    if price <= 0:
        return ""
    precio = Decimal(str(price))
    multiple = Decimal(1) / precio
    return (
        f"Al precio {precio:f}: ×{multiple:.2f} · +{(multiple - 1) * 100:.1f} % "
        "por participación si acierta."
    )


def _colateral_blocker(colateral: Token, limites: ExecutionLimits) -> str | None:
    """El bloqueo por `allowed_tokens` del colateral, o `None` si la política lo deja.

    Estaba escrito tres veces —cobrar, retirar y publicar— y una de las tres
    comparaba en crudo: decía «pUSD no está» mientras enseñaba «PUSD» declarado.
    La comprobación vive ahora en `ExecutionLimits.allows_token`, que normaliza
    los dos lados, y el mensaje —diciendo **qué hay declarado**, porque el error
    más común no es olvidar la lista sino escribir en ella un nombre que ningún
    token tiene— se redacta una sola vez, para los tres caminos.
    """
    if limites.allows_token(colateral.symbol):
        return None
    return (
        f"el colateral «{colateral.symbol}» no está en `allowed_tokens` "
        f"(ahora declara: {', '.join(sorted(limites.allowed_tokens)) or 'nada'}; "
        f"añádelo en config.toml)"
    )


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


def _smallest_marketable_amount(price: Decimal) -> Decimal:
    """El importe redondo más pequeño cuya compra llega al mínimo del recinto.

    Una compra que cruza el libro necesita **importe** ≥ 1 $, y las
    participaciones se derivan del importe redondeando hacia abajo a centésimas
    —es lo que se firma—: hay que encontrar el primer múltiplo de 0,01
    participaciones que valga 1 $ de verdad. A 0,62 $ son 1,62 participaciones
    = 1,0044 $, y el importe que las produce es 1,01 $ (1,01 / 0,62 = 1,6290…
    → 1,62).
    """
    participaciones = (Decimal(1) / price).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )
    return (participaciones * price).quantize(Decimal("0.01"), rounding=ROUND_UP)


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

        # La raíz es un apilador de dos vistas: la **lista** de mercados —lo
        # único que enseña la pestaña— y la **tarjeta** del mercado elegido,
        # que la sustituye al hacer clic en una fila y vuelve con «← Volver a
        # la lista». Es el patrón del recinto, y el motivo de que la pestaña
        # dejara de ser una sola pantalla atestada: elegir y operar son dos
        # momentos distintos.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._views = QStackedWidget()
        root.addWidget(self._views)
        self._list_view = QWidget()
        self._card_view = QWidget()
        self._views.addWidget(self._list_view)
        self._views.addWidget(self._card_view)

        self._build_list_view()
        self._build_card_view()

        # --- Los paneles -------------------------------------------------------
        # Las cestas, el cobro y la wallet dejan de ocupar la página y pasan a
        # ser diálogos: se consultan a ratos, y su alto apilado era el que
        # empujaba fuera de la pantalla lo que sí se usa a diario. Se
        # construyen ya, en `__init__` y no al abrirlos, porque sus widgets
        # (`_baskets`, `_redeem_table`, `_wallet_table`…) tienen que existir
        # para los repintados que llegan aunque el panel esté cerrado —el
        # cambio de modo desde la barra superior, sin ir más lejos—.
        self._baskets_panel = self._build_panel(
            "Cestas con margen", self._build_baskets_card()
        )
        self._redeem_panel = self._build_panel("Por cobrar", self._build_redeem_card())
        self._wallet_panel = self._build_panel(
            "Wallet de depósito", self._build_wallet_card()
        )

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
        # Los selectores de categoría y orden, con sus ítems ya puestos: al
        # abrir la pestaña tienen que ofrecer «Tendencia», «Todas» y los tres
        # órdenes, no estar vacíos hasta la primera búsqueda.
        self._rebuild_categories()
        # Y la tabla de mercados, con su rótulo, desde el primer momento.
        self._fill(())

    def _build_list_view(self) -> None:
        """La lista de mercados: lo que enseña la pestaña al abrirse.

        La búsqueda, su ventana de cierre, la tabla y el acceso a los dos
        paneles que se consultan a ratos. Antes vivían aquí apiladas, además,
        las tarjetas de cestas, cobro y wallet, y eran ellas —tres bloques de
        alto completo— las que dejaban la lista a medio ver.
        """
        lay = ScrollArea.fill(self._list_view, spacing=10).body()

        # Dos filas y no una: con el buscador, los tres selectores y los tres
        # botones en la misma línea, la barra no cabe en el ancho mínimo de la
        # ventana (1100) y los campos se recortan por debajo de lo que su texto
        # necesita —lo que mide la prueba de recorte—. Arriba lo que hace algo
        # —buscar, abrir—; debajo lo que filtra.
        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar en la pregunta… (vacío = todos)")
        self._search.returnPressed.connect(self._on_search)
        top.addWidget(self._search, stretch=1)
        self._btn = QPushButton("Buscar")
        self._btn.clicked.connect(self._on_search)
        top.addWidget(self._btn)

        # Las cestas y el cobro no se miran en cada visita: sus botones viven
        # aquí y sus paneles se abren encima, sin empujar la tabla.
        self._baskets_open_btn = QPushButton("Cestas…")
        self._baskets_open_btn.setObjectName("secondary")
        self._baskets_open_btn.setToolTip(
            "Abre las cestas con margen: mercados donde comprar todos los "
            "resultados cuesta menos de lo que pagan."
        )
        self._baskets_open_btn.clicked.connect(self._open_baskets)
        top.addWidget(self._baskets_open_btn)
        self._redeem_open_btn = QPushButton("Por cobrar…")
        self._redeem_open_btn.setObjectName("secondary")
        self._redeem_open_btn.setToolTip(
            "Abre el cobro: lo que la cartera que firma tiene resuelto y sin "
            "cobrar. Es una transacción por mercado."
        )
        self._redeem_open_btn.clicked.connect(self._open_redeem)
        top.addWidget(self._redeem_open_btn)
        lay.addLayout(top)

        filtros = QHBoxLayout()
        filtros.addWidget(QLabel("Cierran en:"))
        self._window = QComboBox()
        for etiqueta, ventana in _WINDOWS:
            self._window.addItem(etiqueta, ventana)
        self._window.setToolTip(
            "Limita la lista a los mercados que cierran dentro de esa ventana y la "
            "ordena por el reloj. «todas» es la vista por volumen."
        )
        self._window.currentIndexChanged.connect(self._on_window_changed)
        filtros.addWidget(self._window)
        filtros.addWidget(QLabel("Categoría:"))
        self._category = QComboBox()
        self._category.setMinimumWidth(150)
        self._category.setToolTip(
            "Filtra por la categoría del evento, tal cual la publica Polymarket. "
            "«Tendencia» no filtra: ordena por lo que más se mueve en 24 h. "
            "Cambiar de categoría no vuelve a consultar: la próxima búsqueda es "
            "la que la pide a la fuente."
        )
        self._category.currentIndexChanged.connect(self._on_filter_changed)
        filtros.addWidget(self._category)
        filtros.addWidget(QLabel("Orden:"))
        self._sort = QComboBox()
        for etiqueta, criterio in _SORTS:
            # El dato se guarda como texto (el `.value`) y se reconstruye al
            # leerlo: Qt convierte los enums a texto al guardarlos en un ítem.
            self._sort.addItem(etiqueta, criterio.value)
        self._sort.setToolTip(
            "El orden se pide a la fuente en la siguiente búsqueda."
        )
        self._sort.currentIndexChanged.connect(self._on_filter_changed)
        filtros.addWidget(self._sort)
        filtros.addStretch(1)
        lay.addLayout(filtros)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {COLOR_MUTED};")
        lay.addWidget(self._status)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            [
                "Pregunta",
                "Categorías",
                "Favorito",
                "24 h",
                "Total %",
                "Overround",
                "Coherente",
                "Cierra",
            ]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(True)
        # Un mínimo para que la lista se lea: antes la tabla se repartía el alto que
        # quedara y acababa en una franja de dos filas.
        self._table.setMinimumHeight(240)
        # Pulsar una fila abre su tarjeta. La selección se limpia al volver para
        # que la misma fila vuelva a abrirla: ver `_on_back_to_list`.
        self._table.itemSelectionChanged.connect(self._on_select)

        #: El rótulo que ocupa el sitio de la tabla mientras está vacía. Sin él,
        #: al abrir la pestaña queda un rectángulo gris con encabezados y nada
        #: dentro: no se distingue «todavía no has buscado» de «no hay nada».
        self._markets_empty = QLabel("")
        self._markets_empty.setObjectName("empty")
        self._markets_empty.setWordWrap(True)
        self._markets_empty.setAlignment(Qt.AlignCenter)

        lay.addWidget(self._table, stretch=1)
        lay.addWidget(self._markets_empty, stretch=1)

    def _build_card_view(self) -> None:
        """La tarjeta de un mercado: lo que se hace con el que se ha elegido.

        El camino de vuelta va lo primero, porque la pestaña entera cambió por
        él: la lista ya no está debajo, y sin «Volver» la tarjeta sería un
        callejón sin salida.
        """
        lay = ScrollArea.fill(self._card_view, spacing=10).body()

        self._back_btn = QPushButton("← Volver a la lista")
        self._back_btn.setObjectName("secondary")
        self._back_btn.setToolTip(
            "Vuelve a la lista de mercados. La tarjeta no se vacía: si abres el "
            "mismo mercado otra vez, lo que hayas escrito sigue ahí."
        )
        self._back_btn.clicked.connect(self._on_back_to_list)
        fila_volver = QHBoxLayout()
        fila_volver.addWidget(self._back_btn)
        fila_volver.addStretch(1)
        lay.addLayout(fila_volver)

        # Los tres pasos, a la vista y con el que toca encendido. Viven en la
        # tarjeta y no en la lista porque describen lo que se hace **con un
        # mercado**: si esta pantalla está abierta, el paso 1 ya está hecho. El
        # paso encendido sale del estado real, no de un contador que alguien
        # tenga que ir moviendo: ver `_refresh_steps`.
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

        lay.addWidget(self._build_order_card())

    def _build_panel(self, titulo: str, card: Card) -> QDialog:
        """Un diálogo con una tarjeta dentro y un botón para cerrar.

        Es lo que sustituye a apilar secciones en la página: lo que se consulta
        a ratos se abre encima, se cierra, y la lista se queda con el alto. La
        tarjeta llega ya montada y por dentro es la de siempre, así que sus
        `_wallet_*`, `_redeem_*` y `_baskets` siguen colgando de la página y los
        repintados no cambian de dueño.
        """
        panel = QDialog(self)
        panel.setWindowTitle(titulo)
        panel.resize(820, 600)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(card, 1)
        caja = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        caja.rejected.connect(panel.reject)
        lay.addWidget(caja)
        return panel

    def _build_baskets_card(self) -> Card:
        """La tarjeta de las cestas con margen: umbral, tabla y aviso.

        No calcula nada aquí: `_refresh_baskets` recalcula sobre los informes
        ya traídos cuando el umbral cambia o cuando llega una búsqueda nueva, y
        da lo mismo que el panel esté abierto o cerrado —los widgets existen
        desde que la página se construye—.
        """
        card = Card(
            "Cestas con margen",
            subtitle="— comprar todo cuesta menos de lo que paga",
        )
        barra = QHBoxLayout()
        barra.addWidget(QLabel("Umbral:"))
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
        barra.addWidget(self._min_edge)
        self._edge_hint = QLabel("")
        self._edge_hint.setStyleSheet(f"color: {COLOR_MUTED};")
        barra.addWidget(self._edge_hint)
        barra.addStretch(1)
        card.add_row(barra)

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
        card.body().addWidget(self._baskets, stretch=1)

        #: Mismo recurso que en la pestaña de precios: cuando no hay cestas, el
        #: sitio de la tabla lo ocupa la explicación. Sin esto queda un
        #: rectángulo gris del alto entero que no distingue «todavía no has
        #: buscado» de «buscaste y ninguna cesta llega al umbral», que son dos
        #: cosas distintas y se arreglan de forma distinta.
        self._baskets_empty = QLabel("")
        self._baskets_empty.setObjectName("empty")
        self._baskets_empty.setWordWrap(True)
        self._baskets_empty.setAlignment(Qt.AlignCenter)
        card.body().addWidget(self._baskets_empty, stretch=1)

        self._basket_note = QLabel(_BASKET_CAVEAT)
        self._basket_note.setWordWrap(True)
        self._basket_note.setTextFormat(Qt.RichText)
        self._basket_note.setStyleSheet("color: #f5c518; font-size: 11px;")
        card.body().addWidget(self._basket_note)
        return card

    def _on_back_to_list(self) -> None:
        """Vuelve a la lista sin vaciar la tarjeta.

        La selección se limpia para que volver a pulsar **la misma fila**
        vuelva a abrir su tarjeta: `itemSelectionChanged` no se emite si la
        fila ya estaba seleccionada, y volver y querer entrar otra vez en el
        mismo mercado es lo más natural del mundo.
        """
        self._table.clearSelection()
        self._views.setCurrentIndex(0)

    def _open_baskets(self) -> None:
        """Abre el panel de cestas, recalculado con lo último que haya."""
        self._refresh_baskets()
        self._baskets_panel.exec()

    def _open_redeem(self) -> None:
        """Abre el panel del cobro. Leer las posiciones sigue siendo manual."""
        self._redeem_panel.exec()

    def _open_wallet_panel(self) -> None:
        """Abre el panel de la wallet desde el saldo de cualquiera de las caras.

        Si la lectura todavía no se ha hecho, se lanza al abrir el panel, que
        es exactamente donde se viene a mirarla. Abrir un panel no firma nada.
        """
        if not self._wallet_read:
            spawn(self._do_read_wallet())
        self._wallet_panel.exec()

    # ------------------------------------------------------------------ #
    # Búsqueda
    # ------------------------------------------------------------------ #
    def _window_choice(self) -> timedelta | None:
        return self._window.currentData()

    def _category_choice(self) -> MarketTag | None:
        """La etiqueta elegida, o `None` si la vista no filtra por etiqueta.

        Devuelve `None` también para «Tendencia»: esa vista no filtra, ordena, y
        quien decide el orden efectivo es `_effective_sort`.
        """
        dato = self._category.currentData()
        return dato if isinstance(dato, MarketTag) else None

    def _is_trending(self) -> bool:
        """Si la categoría elegida es la vista sintética «Tendencia»."""
        return self._category.currentData() is _TRENDING

    def _sort_choice(self) -> PredictionSort:
        """El orden elegido, reconstruido desde el desplegable.

        Reconstruido y no leído tal cual por la misma razón que en `_side_value`:
        Qt guarda el dato del ítem como texto, así que comparar el valor con un
        `StrEnum` sería compararlo con otra cosa.
        """
        return PredictionSort(self._sort.currentData())

    def _effective_sort(self) -> PredictionSort:
        """El orden que se le pide a la fuente, según la categoría elegida.

        «Tendencia» **es** un orden —lo que más se mueve en 24 h—, así que manda
        sobre el selector; con cualquier otra categoría manda el selector.
        """
        return PredictionSort.TRENDING if self._is_trending() else self._sort_choice()

    def _on_search(self) -> None:
        self._btn.setEnabled(False)
        self._status.setText("Consultando Polymarket…")
        spawn(self._do_search())

    async def _do_search(self) -> None:
        try:
            text = self._search.text().strip() or None
            window = self._window_choice()
            category = self._category_choice()
            sort = self._effective_sort()
            reports = await self._container.analyze_markets(
                limit=30,
                search=text,
                closing_within=window,
                category=category,
                sort=sort,
            )
            self._all_reports = reports
            self._searched = True
            self._last_error = None
            # Las categorías se rellenan con lo que ha llegado **antes** de
            # pintar: el selector tiene que ofrecer lo que hay, conservando en lo
            # posible la selección que hubiera.
            self._rebuild_categories()
            self._show(reports)
            self._status.setText(
                f"{len(self._reports)} mercado(s)"
                + _criteria_text(window, sort, category)
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

    def _on_filter_changed(self) -> None:
        """Aplica categoría y orden sobre lo ya traído, sin pedir nada.

        Es el mismo criterio que la ventana de cierre: un selector no puede
        salir a la red —sería una consulta por cada clic—, así que recorta lo que
        hay, y la **siguiente búsqueda** es la que lleva los filtros a la fuente
        para que el límite se gaste en lo que de verdad se quiere ver.
        """
        self._sync_sort_enabled()
        self._refilter()

    def _sync_sort_enabled(self) -> None:
        """Apaga el selector de orden cuando «Tendencia» manda, diciendo por qué."""
        trending = self._is_trending()
        self._sort.setEnabled(not trending)
        self._sort.setToolTip(
            "«Tendencia» ordena por lo que más se mueve en 24 h; elige otra "
            "categoría para ordenar a tu manera."
            if trending
            else "El orden se pide a la fuente en la siguiente búsqueda."
        )

    def _refilter(self) -> None:
        """Recorta y ordena lo ya traído según ventana, categoría y orden.

        Todo el criterio pasa por las funciones puras del caso de uso: la tabla
        ordena como ordenaría una búsqueda nueva, sin una segunda definición de
        cada orden que se pudiera quedar atrás.
        """
        window = self._window_choice()
        visibles: Sequence[MarketReport] = self._all_reports
        if window is not None:
            now = self._container.clock.now()
            visibles = tuple(
                report
                for report in visibles
                if (left := report.market.time_left(now)) is not None and left <= window
            )
        category = self._category_choice()
        if category is not None:
            visibles = filter_reports_by_tag(visibles, category)
        self._show(sort_reports(visibles, self._effective_sort(), closing_within=window))

    def _on_window_changed(self) -> None:
        """Recorta lo ya traído, sin volver a pedir nada a la fuente.

        Sólo **recorta**: cambiar a una ventana más corta nunca puede añadir
        filas, porque lo que no se trajo no está. Con «todas» se recupera la
        lista entera tal como llegó, que es lo que hace que el selector sea
        reversible y se pueda probar sin coste.
        """
        self._refilter()

    def _rebuild_categories(self) -> None:
        """Rellena el selector de categorías con las etiquetas de lo traído.

        «Tendencia» va primera —es la vista de lo que más se mueve—, «Todas»
        después, y luego las etiquetas reales **tal cual las publica la fuente**,
        ordenadas por número de mercados: las que organizan la lista quedan
        arriba y las que acompañan a dos mercados, abajo. La selección se
        conserva por slug; si la etiqueta elegida ya no está en los resultados,
        su ítem se reinserta —cambiarle la búsqueda al usuario por debajo sería
        dejarlo filtrado por algo que ya no ve—.
        """
        actual = self._category.currentData()
        conteo: dict[str, int] = {}
        etiquetas: dict[str, MarketTag] = {}
        for report in self._all_reports:
            for tag in report.market.tags:
                conteo[tag.slug] = conteo.get(tag.slug, 0) + 1
                etiquetas.setdefault(tag.slug, tag)
        orden = sorted(
            etiquetas.values(), key=lambda tag: (-conteo[tag.slug], tag.label.lower())
        )
        if isinstance(actual, MarketTag) and actual.slug not in etiquetas:
            orden.insert(0, actual)

        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem("Tendencia", _TRENDING)
        self._category.addItem("Todas", None)
        for tag in orden:
            self._category.addItem(f"{tag.label} ({conteo.get(tag.slug, 0)})", tag)
        self._category.setCurrentIndex(_category_index(self._category, actual))
        self._category.blockSignals(False)
        self._sync_sort_enabled()

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
        # El importe vuelve a su valor por omisión: es el tamaño con el que
        # nace una tarjeta, y el que hubiera era del mercado anterior.
        self._amount.blockSignals(True)
        self._amount.setValue(float(_DEFAULT_BUY_AMOUNT))
        self._amount.blockSignals(False)
        self._sync_buy_shares()
        self._order_market.setText("Selecciona un mercado de la lista.")
        self._trend_label.setText("")
        self._stats_label.setText("")
        self._tags_label.setText("")
        self._detail.setText("")
        self._order_cost.setText("—")
        self._order_payout.setText("")
        self._payout_label.setText("")
        self._book_label.setText("")
        self._market_limits.setText("")
        self._order_status.setText("")
        self._refresh_order_state()

    def _fill(self, reports: tuple[MarketReport, ...]) -> None:
        now = self._container.clock.now()
        self._table.setRowCount(0)
        self._clear_order_card()
        # Una lista nueva —búsqueda nueva o cambio de ventana— devuelve a la
        # vista de lista: dejar abierta la tarjeta de un mercado que ya no está
        # en la tabla sería operar sobre lo que no se está mirando.
        self._views.setCurrentIndex(0)
        for rep in reports:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(rep.question))
            categories = " · ".join(tag.label for tag in rep.market.tags) or "—"
            category_item = QTableWidgetItem(categories)
            if rep.market.tags:
                category_item.setToolTip(
                    "Categorías del evento: " + " · ".join(tag.label for tag in rep.market.tags)
                )
            else:
                category_item.setToolTip("La fuente no publica categorías para este mercado")
            self._table.setItem(row, 1, category_item)
            self._table.setItem(row, 2, QTableWidgetItem(f"{rep.favourite.label} ({rep.favourite.implied_percent:.1f} %)"))
            change = rep.market.price_change_24h
            change_item = QTableWidgetItem(_cents(change))
            if change is not None and change != 0:
                change_item.setForeground(QColor(COLOR_SUCCESS if change > 0 else COLOR_DANGER))
            change_item.setToolTip(
                "Cuánto se ha movido el precio en las últimas 24 h, por participación"
            )
            self._table.setItem(row, 3, change_item)
            self._table.setItem(row, 4, QTableWidgetItem(f"{rep.total_percent:.2f} %"))
            self._table.setItem(row, 5, QTableWidgetItem(str(rep.overround_bps)))
            self._table.setItem(row, 6, QTableWidgetItem("✓" if rep.is_coherent else "✗"))
            closes_at = rep.market.closes_at
            item = QTableWidgetItem(countdown(closes_at, now))
            # La fecha exacta sigue disponible: la cuenta atrás es para leer la
            # tabla de un vistazo, no para esconder el dato.
            item.setToolTip(
                closes_at.isoformat() if closes_at is not None else "La fuente no publica fecha de cierre"
            )
            self._table.setItem(row, 7, item)
        self._table.resizeRowsToContents()
        # Igual que en las cestas: al abrir la pestaña la tabla de mercados era un
        # rectángulo gris del alto entero con los encabezados encima y nada
        # dentro, que no se distingue de una búsqueda sin resultados.
        set_empty(self._table, self._markets_empty, self._why_no_rows())

    def _why_no_rows(self) -> str:
        """Por qué la tabla de mercados está vacía, en el caso que sea.

        Son cinco situaciones distintas y cada una se arregla de una forma: no
        haber buscado (pulsa Buscar), que la búsqueda fallara (mira arriba), que
        la fuente no tenga nada para esa palabra (cámbiala), que la categoría
        elegida no deje ninguna fila (ponla en «Todas») y que el filtro de
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
        category = self._category_choice()
        if category is not None:
            return (
                f"Ninguno de los {len(self._all_reports)} mercados traídos es de "
                f"la categoría «{category.label}». Ponla en «Todas» para verlos "
                "todos, elige otra, o vuelve a buscar para que se pida al origen."
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
        # Y la tarjeta sustituye a la lista: elegir y operar son dos momentos,
        # y el segundo empieza aquí.
        self._views.setCurrentIndex(1)

    def _fill_order_card(self, market: PredictionMarket) -> None:
        """Carga el mercado elegido en la tarjeta, y pide su libro.

        Se reconstruye entera y no se parchea campo a campo: los límites del
        campo de precio —el salto, los decimales, el rango— son **del mercado**,
        y dejar los del anterior mientras se cambia de fila deja escribir durante
        un instante una orden que el recinto rechazaría.
        """
        vence = countdown(market.closes_at, self._container.clock.now())
        sufijo = "" if vence == "—" else f" · cierra {vence}"
        self._order_market.setText(f"<b>{market.question}</b>{sufijo}")
        self._order_market.setTextFormat(Qt.RichText)
        self._trend_label.setText(_trend_html(market))
        self._stats_label.setText(_stats_text(market))
        self._tags_label.setText(_tags_text(market))
        self._order_status.setText("")
        self._chosen_market = market
        self._depth = None
        # Mercado nuevo, propuesta nueva: el precio que hubiera escrito para el
        # mercado anterior no dice nada de éste.
        self._price_auto = True
        # Y el importe vuelve a su valor por omisión: el mercado nuevo estrena
        # tamaño. Las participaciones derivadas se recalculan al final, cuando
        # el precio propuesto ya es el de este mercado.
        self._amount.blockSignals(True)
        self._amount.setValue(float(_DEFAULT_BUY_AMOUNT))
        self._amount.blockSignals(False)

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
        # El saldo de la wallet decide si una compra cabe, así que se lee una
        # vez por sesión al abrir la primera tarjeta, sin esperar a que nadie
        # lo pida. Después lo refrescan el panel, las órdenes publicadas y
        # «Añadir saldo». Sin cartera falla al instante, sin salir a la red.
        if not self._wallet_read:
            spawn(self._do_read_wallet())

    # ------------------------------------------------------------------ #
    # Operar: la tarjeta de orden
    # ------------------------------------------------------------------ #
    def _build_order_card(self) -> Card:
        """La tarjeta que convierte un mercado leído en una orden firmable.

        Tiene el aspecto de un panel de operación: el lado, el resultado, el
        precio y el tamaño se eligen con botones. Los combos `_outcome` y `_side`
        siguen siendo la fuente de verdad: los botones sólo los reflejan, así que
        la lógica de precio, libro y límites no cambia.

        Las dos caras no son simétricas a propósito: se **compra por importe**
        —y las participaciones se derivan, hacia abajo— y se **vende por
        participaciones**, porque cada lado piensa en la cifra que compromete:
        el comprador, en el dinero; el vendedor, en lo que entrega.
        """
        card = Card("Operar", subtitle="— orden límite")
        # Ancho mínimo de verdad y **sin tope**: el tope de 460 px era el que
        # aplastaba los campos y dejaba la tarjeta ilegible justo donde se escriben
        # las cifras que se van a firmar.
        card.setMinimumWidth(400)

        self._order_chip = Chip("", COLOR_MUTED)
        card.header.addWidget(self._order_chip)

        self._order_market = QLabel("Selecciona un mercado de la lista.")
        self._order_market.setObjectName("hint")
        self._order_market.setWordWrap(True)
        card.body().addWidget(self._order_market)

        # La tendencia del precio y las cifras de actividad, bajo la pregunta:
        # es lo que se mira para decidir si el mercado se está moviendo y si hay
        # con qué operarlo, y en la lista no cabía con esta calma.
        self._trend_label = QLabel("")
        self._trend_label.setTextFormat(Qt.RichText)
        self._trend_label.setWordWrap(True)
        card.body().addWidget(self._trend_label)

        self._stats_label = QLabel("")
        self._stats_label.setObjectName("hint")
        self._stats_label.setWordWrap(True)
        card.body().addWidget(self._stats_label)

        self._tags_label = QLabel("")
        self._tags_label.setObjectName("hint")
        self._tags_label.setWordWrap(True)
        card.body().addWidget(self._tags_label)

        # La nota del informe —overround, coherencia— y el porcentaje de cada
        # resultado: lo que hace falta saber **antes** de escribir la orden. En
        # la lista era una línea que se perdía en cuanto la tabla crecía.
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(f"color: {COLOR_MUTED};")
        card.body().addWidget(self._detail)

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
        card.add_row(fila_lado)
        self._mark_side_buttons()

        self._outcome_row = QHBoxLayout()
        self._outcome_row.setSpacing(8)
        self._outcome_buttons: list[QPushButton] = []
        card.add_row(self._outcome_row)

        # El precio límite va fuera de las caras: es de la **orden**, no del
        # lado —comprar y vender a un precio es la misma decisión— y duplicarlo
        # serían dos campos para el mismo dato.
        self._price = AmountSpinBox()
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
        fila_campo_precio = QHBoxLayout()
        fila_campo_precio.addWidget(campo_precio, 1)
        card.add_row(fila_campo_precio)

        # --- Cara de compra: el importe manda --------------------------------
        self._buy_box = QWidget()
        caja_compra = QVBoxLayout(self._buy_box)
        caja_compra.setContentsMargins(0, 0, 0, 0)
        caja_compra.setSpacing(8)

        self._amount = AmountSpinBox()
        self._amount.setDecimals(2)
        self._amount.setRange(0.01, 1_000_000.0)
        self._amount.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._amount.setToolTip(
            "Cuánto dinero pones, en el colateral del recinto. Las "
            "participaciones se derivan de este importe y del precio, siempre "
            "hacia abajo: nunca compras más de lo que el importe paga."
        )
        self._amount.valueChanged.connect(self._on_amount_edited)
        fila_importe = QHBoxLayout()
        fila_importe.setSpacing(6)
        fila_importe.addWidget(self._amount, 1)
        campo_importe = Field("IMPORTE")
        campo_importe.add(_contenedor(fila_importe), 1)
        caja_compra.addWidget(campo_importe)

        fila_chips_compra = QHBoxLayout()
        fila_chips_compra.setSpacing(6)
        for importe in _BUY_AMOUNTS:
            atajo = QPushButton(f"${importe:f}")
            atajo.setObjectName("secondary")
            atajo.clicked.connect(lambda _c=False, a=importe: self._set_amount(a))
            fila_chips_compra.addWidget(atajo)
        fila_chips_compra.addStretch(1)
        caja_compra.addLayout(fila_chips_compra)

        #: Las participaciones que salen del importe al precio actual. Se
        #: enseñan porque son **lo que se firma**: el importe es la forma de
        #: escribirlas, no el tamaño de la orden.
        self._buy_derived = QLabel("")
        self._buy_derived.setObjectName("hint")
        self._buy_derived.setWordWrap(True)
        caja_compra.addWidget(self._buy_derived)

        fila_saldo_compra, self._wallet_balance_buy, self._wallet_open_btn_buy = (
            self._wallet_row()
        )
        caja_compra.addLayout(fila_saldo_compra)
        card.body().addWidget(self._buy_box)

        # --- Cara de venta: las participaciones mandan -----------------------
        self._sell_box = QWidget()
        caja_venta = QVBoxLayout(self._sell_box)
        caja_venta.setContentsMargins(0, 0, 0, 0)
        caja_venta.setSpacing(8)

        self._shares = AmountSpinBox()
        self._shares.setDecimals(2)
        self._shares.setRange(0.01, 1_000_000.0)
        self._shares.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._shares.setToolTip(
            "Participaciones. Cada una paga 1 del colateral si el resultado "
            "ocurre, y 0 si no."
        )
        self._shares.valueChanged.connect(self._on_order_edited)
        fila_participaciones = QHBoxLayout()
        fila_participaciones.setSpacing(6)
        fila_participaciones.addWidget(self._shares, 1)
        campo_participaciones = Field("PARTICIPACIONES")
        campo_participaciones.add(_contenedor(fila_participaciones), 1)
        caja_venta.addWidget(campo_participaciones)

        fila_chips_venta = QHBoxLayout()
        fila_chips_venta.setSpacing(6)
        for cantidad in _SELL_SHARES:
            atajo = QPushButton(str(cantidad))
            atajo.setObjectName("secondary")
            atajo.clicked.connect(
                lambda _c=False, n=cantidad: self._shares.setValue(float(n))
            )
            fila_chips_venta.addWidget(atajo)
        fila_chips_venta.addStretch(1)
        caja_venta.addLayout(fila_chips_venta)

        fila_saldo_venta, self._wallet_balance_sell, self._wallet_open_btn_sell = (
            self._wallet_row()
        )
        caja_venta.addLayout(fila_saldo_venta)
        card.body().addWidget(self._sell_box)

        # Las reglas del mercado —mínimo de participaciones y salto de precio—
        # a la vista **siempre**, y fuera de las dos caras: lo necesitan las dos
        # —al vender y, al comprar, cuando la orden descansa en el libro— y
        # dentro de una cara la otra lo escondería justo al cambiar de lado.
        # Antes sólo se nombraba en la línea derivada de la compra, así que al
        # vender no había forma de saberlo sin que algo fallara.
        self._market_limits = QLabel("")
        self._market_limits.setObjectName("hint")
        self._market_limits.setWordWrap(True)
        self._market_limits.setToolTip(
            "Lo publica la fuente para cada mercado: el mínimo de participaciones "
            "por orden y el salto al que se mueve el precio. Cambian de un "
            "mercado a otro. El mínimo rige las órdenes que descansan en el "
            "libro y las ventas; una compra que cruza el libro la valida el "
            "recinto por importe (mínimo 1 $), y la tarjeta lo comprueba con la "
            "cifra exacta."
        )
        card.body().addWidget(self._market_limits)

        # Nace en compra: la cara de venta se enseña al cambiar de lado. El
        # `setVisible` es explícito para que la cara oculta esté oculta **por
        # algo** y no por accidente del layout.
        self._buy_box.setVisible(True)
        self._sell_box.setVisible(False)

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

        # Cuánto paga una participación al precio escrito: el «×1.61 · +61 %» que
        # se mira para comparar mercados. Depende del **precio**, no del tamaño,
        # así que va debajo del pago total y se recalcula con él.
        self._payout_label = QLabel("")
        self._payout_label.setObjectName("hint")
        self._payout_label.setWordWrap(True)
        card.body().addWidget(self._payout_label)

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

    def _wallet_row(self) -> tuple[QHBoxLayout, QLabel, QPushButton]:
        """La fila de saldo que llevan las dos caras: la cifra y su botón.

        El saldo va junto al formulario porque es la cifra que decide si una
        compra cabe —y cuántas participaciones hay para vender—, y esconderla
        en un panel sería esconder justo el dato que hace falta para escribir
        el importe. El botón abre ese panel: transferir desde la cartera,
        recibir desde fuera con el QR, y las posiciones con sus acciones.

        Se construye una vez por cara y devuelve su etiqueta y su botón, para
        que `_refresh_wallet_balance_labels` escriba las dos con el mismo dato
        y los tests no tengan que buscar entre los hijos.
        """
        fila = QHBoxLayout()
        fila.setSpacing(6)
        rotulo = QLabel("Saldo wallet")
        rotulo.setObjectName("hint")
        fila.addWidget(rotulo)
        saldo = QLabel("—")
        saldo.setWordWrap(True)
        fila.addWidget(saldo, 1)
        boton = QPushButton("Operar wallet…")
        boton.setObjectName("secondary")
        boton.setToolTip(
            "Abre el panel de la deposit wallet: su saldo y sus posiciones, añadir "
            "saldo desde tu cartera y recibir desde fuera con el QR. No firma nada."
        )
        boton.clicked.connect(self._open_wallet_panel)
        fila.addWidget(boton)
        return fila, saldo, boton

    def _set_amount(self, importe: Decimal) -> None:
        """Un atajo de importe: pone el valor y el resto sale de las señales."""
        self._amount.setValue(float(importe))

    def _on_amount_edited(self) -> None:
        """El importe cambió: se derivan las participaciones y se repinta."""
        self._sync_buy_shares()
        self._refresh_order_state()

    def _sync_buy_shares(self) -> None:
        """Deriva las participaciones del importe escrito. Sólo en compra.

        `_shares` sigue siendo **la** fuente del tamaño —es lo que firma
        `_do_submit`— y el importe es la forma de escribirlo. Se redondea
        hacia **abajo** a los dos decimales del campo: hacia arriba, las
        participaciones resultantes costarían más de lo que el importe paga.
        Sin señales para no disparar `_on_order_edited` en bucle: el mismo
        patrón que usa `_propose_price` con el precio.
        """
        if not self._is_buy():
            return
        precio = Decimal(str(self._price.value()))
        if precio <= 0:
            return
        importe = Decimal(str(self._amount.value()))
        participaciones = (importe / precio).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        self._shares.blockSignals(True)
        self._shares.setValue(float(participaciones))
        self._shares.blockSignals(False)

    def _refresh_buy_derived(self) -> None:
        """La línea que traduce el importe a participaciones.

        Va bajo los atajos porque contesta a «¿cuántas me llevo por esto?». El
        mínimo del mercado no se repite aquí: vive en la línea de reglas de la
        tarjeta, debajo de las dos caras, desde donde también se lee al vender.
        """
        if not self._is_buy():
            self._buy_derived.setText("")
            return
        market = self._market()
        if market is None:
            self._buy_derived.setText("")
            return
        participaciones = Decimal(str(self._shares.value()))
        self._buy_derived.setText(f"= {participaciones:f} participaciones")

    def _refresh_market_limits(self) -> None:
        """Las reglas del mercado —mínimo y salto—, siempre a la vista.

        Se dicen con lo que publica la fuente, sin los valores de socorro que
        usan los campos —1 y 0.01— para poder existir siquiera: un dato que no
        consta se dice que no consta, porque enseñar el de socorro sería hacer
        pasar por regla del recinto lo que es una elección nuestra. Los mismos
        valores que vigilan `tradeability_blockers` al firmar.
        """
        market = self._market()
        if market is None:
            self._market_limits.setText("")
            return
        partes: list[str] = []
        minimo = market.min_order_size
        if minimo is not None:
            partes.append(f"mínimo del mercado: {minimo:f} participaciones")
        else:
            partes.append("no consta el mínimo de participaciones")
        tick = market.tick_size
        if tick is not None:
            partes.append(f"salto de precio: {tick:f}")
        else:
            partes.append("no consta el salto de precio")
        self._market_limits.setText(" · ".join(partes))

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
            if not limites.allows_chain(cadena):
                motivos.append(
                    f"la red «{cadena}» no está en `allowed_chains` "
                    f"(ahora declara: {', '.join(sorted(limites.allowed_chains)) or 'nada'})"
                )
            try:
                colateral = redeemer.collateral_on(cadena)
            except Exception as error:
                motivos.append(f"no se pudo determinar el colateral del recinto: {error}")
            else:
                motivo = _colateral_blocker(colateral, limites)
                if motivo is not None:
                    motivos.append(motivo)
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
        # Y el saldo, a las dos caras de la tarjeta: es el mismo dato y no
        # puede quedarse viejo en una de ellas.
        self._refresh_wallet_balance_labels()

    def _refresh_wallet_balance_labels(self) -> None:
        """El saldo de la wallet, en las dos caras de la tarjeta del mercado.

        Es la cifra que decide si una compra cabe —y cuántas participaciones
        se pueden vender—, así que vive junto al formulario. Sin lectura
        todavía, o con error, va un guion con el motivo en el tooltip: un cero
        inventado se leería como «no tienes nada» y sería falso.
        """
        view = self._wallet_view
        if view is None:
            texto = "—"
            motivo = self._wallet_error or "Todavía no se ha leído la wallet."
        else:
            texto = f"{format_amount(view.balance.as_decimal())} {view.collateral.symbol}"
            motivo = (
                "El saldo de tu deposit wallet de Polymarket. «Operar wallet…» "
                "abre el panel para leerlo, añadir saldo o ver el QR."
            )
        for etiqueta in (self._wallet_balance_buy, self._wallet_balance_sell):
            etiqueta.setText(texto)
            etiqueta.setToolTip(motivo)

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
                "Cierra este panel y carga la posición en la tarjeta del mercado "
                "para venderla. No firma nada: revisa el precio y publica allí."
            )
            comprar.setToolTip(
                "Cierra este panel y carga el mercado en su tarjeta para comprar "
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
        # Y el saldo, a las dos caras de la tarjeta del mercado: si la clave o
        # el modo cambian, el repintado llega por aquí.
        self._refresh_wallet_balance_labels()

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
        if not limites.allows_chain(SETTLEMENT_WALLET_CHAIN):
            motivos.append(
                f"la red «{SETTLEMENT_WALLET_CHAIN}» no está en `allowed_chains` "
                f"(ahora declara: {', '.join(sorted(limites.allowed_chains)) or 'nada'})"
            )
        try:
            colateral = container.read_settlement_wallet.collateral()
        except Exception as error:
            motivos.append(f"no se pudo determinar el colateral del recinto: {error}")
        else:
            motivo = _colateral_blocker(colateral, limites)
            if motivo is not None:
                motivos.append(motivo)
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
            self._wallet_panel,
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
            parent=self._wallet_panel,
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

        minimo = market.min_order_size or Decimal(1)
        cantidad: Decimal | None = None
        if side is PredictionSide.SELL:
            # Suelo a dos decimales —los del campo— y **hacia abajo**: el widget
            # redondearía hacia arriba y propondría vender más participaciones de
            # las que hay.
            cantidad = posicion.shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if cantidad < minimo:
                self._order_status.setText(
                    f"Tu posición es de {posicion.shares:f} participaciones y el "
                    f"mercado pide un mínimo de {minimo} por orden: la venta entera "
                    "no se puede publicar. «Comprar más» sí está disponible para "
                    "llegar al mínimo."
                )
                return

        # El panel de la wallet se cierra y la vista salta a la tarjeta: lo que
        # sigue —cargar el mercado, revisar el precio, publicar— pasa allí, y un
        # aviso de error al lado de la tarjeta no se leería con el panel encima.
        self._wallet_panel.accept()
        self._views.setCurrentIndex(1)
        self._table.clearSelection()
        self._detail.setText("")
        self._fill_order_card(market)
        indice = self._outcome.findData(resultado.label)
        if indice >= 0:
            self._outcome.setCurrentIndex(indice)
        self._side.setCurrentIndex(0 if side is PredictionSide.BUY else 1)
        if cantidad is None:
            # Se compra por importe: el mínimo del mercado se pone **en
            # dinero**, redondeado hacia arriba a los céntimos del campo, para
            # que las participaciones derivadas lleguen al mínimo sin que nadie
            # tenga que calcularlo.
            precio = Decimal(str(self._price.value()))
            importe = (minimo * precio).quantize(Decimal("0.01"), rounding=ROUND_UP)
            self._amount.setValue(float(importe))
            self._sync_buy_shares()
        else:
            self._shares.setValue(float(cantidad))
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
        """Cambiar de lado cambia la cara —y **vuelve a proponer el precio**, lo
        que es deliberado.

        El precio propuesto sale del libro del lado en el que se está: al comprar
        el mejor `ask`, al vender el mejor `bid`. Conservar el de la compra en una
        venta dejaría una orden que no se cruza —que en una orden límite significa
        quedarse mirando—, y el campo no es una decisión del usuario todavía: es
        una propuesta que él puede cambiar después. Lo que no puede es quedarse
        con una propuesta que ya no corresponde a lo que va a firmar.

        Y la cara cambia con el lado porque los dos lados se escriben distinto:
        comprar es elegir cuánto dinero; vender, cuántas participaciones.
        """
        self._price_auto = True
        self._mark_side_buttons()
        self._buy_box.setVisible(self._is_buy())
        self._sell_box.setVisible(not self._is_buy())
        self._apply_market_limits()
        self._refresh_order_state()

    def _on_price_edited(self) -> None:
        """El usuario escribió un precio: deja de ser una propuesta y es suyo.

        Se marca aquí y no comparando el valor con el propuesto porque pueden
        coincidir —bajar el precio al del libro es exactamente lo que alguien
        haría—, y en ese caso la intención del usuario tiene que ganar. En
        compra, además, el tamaño derivado cambia con el precio: las mismas
        participaciones tienen que seguir cabiendo en el mismo importe.
        """
        self._price_auto = False
        self._sync_buy_shares()
        self._refresh_order_state()

    def _on_order_edited(self) -> None:
        self._refresh_order_state()

    def _apply_market_limits(self) -> None:
        """Ajusta los campos a las reglas **de este mercado**.

        El mínimo de participaciones y el salto de precio los publica la fuente
        y cambian de un mercado a otro; un campo con los límites de otro
        mercado deja escribir una orden que el recinto rechaza entera, y
        descubrirlo al firmar es descubrirlo después de haberla confirmado.

        El mínimo se aplica distinto por lado: al **vender** el campo se
        recorta hacia arriba al mínimo del mercado —vender menos de eso no se
        puede publicar—, y al **comprar** el campo baja a 0,01 y el mínimo lo
        vigila el bloqueo con su motivo escrito, con la regla que toque
        —importe si la compra cruza el libro, participaciones si descansa—:
        subirle las participaciones al usuario sería comprar más de lo que
        escribió en el importe.
        """
        market = self._market()
        if market is None:
            return
        self._refresh_market_limits()
        minimo = market.min_order_size or Decimal(1)
        tick = market.tick_size or Decimal("0.01")
        if self._is_buy():
            self._shares.setMinimum(0.01)
        else:
            self._shares.setMinimum(float(minimo))
            if self._shares.value() < float(minimo):
                self._shares.setValue(float(minimo))
        self._price.setDecimals(_decimals_of(tick))
        self._price.setSingleStep(float(tick))
        self._price.setRange(float(tick), float(Decimal(1) - tick))
        self._propose_price()
        # El precio propuesto acaba de cambiar —o de fijarse—, y en compra el
        # tamaño sale de él: sin esto, cambiar de mercado o de libro dejaría
        # unas participaciones derivadas de un precio que ya no está.
        self._sync_buy_shares()

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
            self._payout_label.setText("")
            self._book_label.setText("")
            self._refresh_buy_derived()
            return

        colateral = self._collateral()
        symbol = colateral.symbol if colateral is not None else "colateral"
        participaciones = Decimal(str(self._shares.value()))
        if self._is_buy():
            # El coste se enseña exacto —participaciones × precio, sin
            # redondear—: son las dos cifras que se firman, y el importe
            # escrito es sólo su cociente redondeado hacia abajo.
            self._order_cost.setText(f"Pagas {self._cost()} {symbol}")
            self._order_payout.setText(
                f"= {participaciones:f} participaciones · pagan {participaciones:f} "
                f"{symbol} si acierta el resultado, y 0 si no."
            )
            self._submit_btn.setText("Realizar orden de compra")
        else:
            self._order_cost.setText(f"Cobras {self._cost()} {symbol}")
            self._order_payout.setText(
                f"{participaciones:f} participaciones pagan {participaciones:f} "
                f"{symbol} si acierta el resultado, y 0 si no."
            )
            self._submit_btn.setText("Realizar orden de venta")
        self._payout_label.setText(_payout_text(self._price.value()))
        self._refresh_buy_derived()
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
            falta = "Vuelve a la lista y elige el mercado que quieras operar."
        elif self._depth is None:
            actual = 1
            falta = "Elige resultado, lado y tamaño; el libro del resultado se está leyendo."
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
        if not limites.allows_chain(cadena):
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
                motivo = _colateral_blocker(colateral, limites)
                if motivo is not None:
                    motivos.append(motivo)
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
        """Lo que hace inválida **esta** orden, no la configuración.

        El mínimo del mercado no es «participaciones» a secas: cuál de las dos
        reglas del recinto aplica lo decide el libro. Una compra que **cruza**
        el libro se valida por **importe** —medido: «invalid amount for a
        marketable BUY order …, min size: $1»—, y el mínimo de participaciones
        es para las órdenes que **descansan** en el libro; las ventas también
        se validaron por tamaño (una venta que cruza de 5 participaciones =
        0,75 $ se aceptó). Exigirlo todo por participaciones apagaba compras
        que el recinto sí acepta —el caso de «no me deja comprar 1 $»— y dejaba
        pasar compras que rechazará: 5 participaciones a 0,18 $ son 0,90 $.
        """
        motivos: list[str] = []
        shares = Decimal(str(self._shares.value()))
        precio = Decimal(str(self._price.value()))
        minimo = market.min_order_size
        if self._is_buy() and self._crosses_book(precio):
            importe = shares * precio
            if importe < _MIN_MARKETABLE_BUY_AMOUNT:
                motivos.append(
                    f"el recinto pide un mínimo de {_MIN_MARKETABLE_BUY_AMOUNT:f} $ "
                    f"de importe para una compra que cruza el libro, y este importe "
                    f"se queda en {importe:f} $; sube el importe a "
                    f"{_smallest_marketable_amount(precio):f} $"
                )
        elif minimo is not None and shares < minimo:
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

    def _crosses_book(self, precio: Decimal) -> bool:
        """Si una compra a este precio encontrará contrapartida ya en el libro.

        Cruza cuando el precio alcanza el mejor `ask` del resultado elegido.
        Sin libro leído no se puede afirmar —la lectura es de red y puede
        haber fallado— y se responde que no: entonces manda la regla del
        tamaño, que es la que no depende del libro.
        """
        if self._depth is None:
            return False
        best_ask = self._depth.best_ask
        return best_ask is not None and precio >= best_ask

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
        """Recalcula las cestas sobre los informes ya traídos. Sin red.

        Repinta también con el panel cerrado: los widgets existen desde que la
        página se construye, y así abrirlo no tiene que recalcular nada.
        """
        self._update_edge_hint()
        self._baskets.setRowCount(0)
        if not self._reports:
            set_empty(
                self._baskets,
                self._baskets_empty,
                "Busca mercados en la lista: las cestas se calculan sobre los "
                "que hayan salido.",
            )
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
