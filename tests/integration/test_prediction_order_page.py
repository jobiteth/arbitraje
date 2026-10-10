"""La tarjeta de orden de la pestaña de predicción: que exista, y qué la apaga.

Esta prueba nace de un hueco medido, no de una idea: la pestaña leía mercados
reales de Polymarket y **no ofrecía ninguna forma de comprar ni de vender**. Era
una pestaña que describía un mercado en el que no se podía participar. Lo que se
fija aquí es que ese camino existe y que dice la verdad sobre por qué no deja
usarlo.

### Por qué con un contenedor real y un motor falseado

El motor está falseado —devuelve mercados operables y un libro conocido— porque
lo que se prueba no es Polymarket: es que la vista lea **sus** reglas —el mínimo
de participaciones, el salto de precio, el libro— y que la lista de motivos mire
lo mismo que mira el caso de uso. Todo lo demás es real: el `Container`, el
registro, la política con sus límites y los widgets de Qt.

Doblar la vista no serviría: la mitad de lo que se comprueba es justo lo que la
vista le pasa al caso de uso.

Qt necesita un `QApplication` vivo, y en una máquina sin pantalla se resuelve con
la plataforma `offscreen`, que se fija aquí antes de que se importe nada de Qt.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from amigocompora.app.container import Container, build_container
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    DepthLevel,
    MarketDepth,
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionOrder,
    PredictionSide,
    PredictionSort,
    SubmittedPredictionOrder,
    Token,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import (
    PRIVATE_KEY_SECRET,
    InMemorySecretStore,
    app_secret_key,
)
from amigocompora.ui.pages.prediction import PredictionPage, _round_to_tick

#: La clave de desarrollo **publicada** (la primera de Hardhat). No es un
#: secreto: que sea conocida es lo que permite afirmar sobre la dirección
#: derivada sin que la calcule el código que se está probando.
CLAVE_DE_DESARROLLO = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DIRECCION = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

#: El colateral del recinto, tal como lo nombra el dominio. Es la stablecoin
#: puenteada de Polygon: el `symbol()` de las dos es «USDC» y se distinguen por
#: dirección, así que aquí lo que importa es el símbolo, que es lo que la lista
#: blanca compara.
COLATERAL = Token(
    symbol="USDC",
    decimals=6,
    chain="polygon",
    address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
)

VENUE = Venue(
    venue_id="falso", name="Falso", kind=VenueKind.PREDICTION_MARKET, chain="polygon"
)

_SALTO = Decimal("0.01")
_MINIMO = Decimal("5")

#: Identificadores de resultado, correlativos y únicos: el `tokenId` de un
#: ERC-1155 es un `uint256` en decimal, y el dominio lo exige así.
_RESULTADOS = count(1001)


class MotorFalso:
    """Un recinto de mentira que sabe leer, construir, firmar y publicar.

    Implementa el protocolo **entero** de escritura y no sólo la parte que cada
    prueba toca: `EngineRegistry.prediction_planner` comprueba el tipo, y un
    doble al que le faltara `sign_order` haría fallar la prueba por el doble en
    vez de por lo que se está midiendo.
    """

    def __init__(self) -> None:
        self.libros_pedidos: list[str] = []
        self.firmadas: list[PredictionOrder] = []
        self.publicadas: list[PredictionOrder] = []
        # El colateral es un atributo y no una constante leída directa: hay una
        # prueba que lo pone en caja mixta (`pUSD`, como el catálogo) para medir
        # la comprobación de la lista blanca sin tocar el resto del doble.
        self.colateral: Token = COLATERAL

    @property
    def manifest(self) -> EngineManifest:
        return EngineManifest(
            engine_id="falso",
            name="Recinto falso",
            version="1.0.0",
            kind=EngineKind.PREDICTION_MARKETS,
            summary="Un recinto que sabe operar, para probar la tarjeta de orden.",
            capabilities=frozenset(),
            required_config=(),
            allowed_hosts=(),
        )

    async def aopen(self) -> None: ...
    async def aclose(self) -> None: ...

    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
        category: MarketTag | None = None,
        sort: PredictionSort | None = None,
    ) -> Sequence[PredictionMarket]:
        del search, closing_within, category, sort
        return tuple(_MERCADOS[:limit])

    async def market(self, market_id: str) -> PredictionMarket:
        return next(m for m in _MERCADOS if m.market_id == market_id)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        """El mercado de una posición, por su `conditionId`.

        Aquí no lo pide nadie —esta prueba no toca la wallet de depósito—, pero
        el doble tiene que saberlo hacer para seguir siendo un motor de
        predicción: el protocolo lo exige y el registro lo comprueba.
        """
        return next(m for m in _MERCADOS if m.condition_id == condition_id)

    async def book(self, token_id: str) -> MarketDepth:
        """Un libro de dos niveles, con la venta por encima y la compra por debajo.

        Los dos niveles no son decoración: con uno solo, «cruzar el libro» y
        «coger el mejor precio» darían la misma cifra y la prueba no distinguiría
        un cálculo que recorre la profundidad de uno que sólo lee el primer
        nivel, que es justo la diferencia que el panel existe para enseñar.
        """
        self.libros_pedidos.append(token_id)
        for market in _MERCADOS:
            for outcome in market.outcomes:
                if outcome.token_id == token_id:
                    return MarketDepth(
                        token_id=token_id,
                        bids=(
                            DepthLevel(price=Decimal("0.58"), size=Decimal("10")),
                            DepthLevel(price=Decimal("0.50"), size=Decimal("100")),
                        ),
                        asks=(
                            DepthLevel(price=Decimal("0.62"), size=Decimal("10")),
                            DepthLevel(price=Decimal("0.70"), size=Decimal("100")),
                        ),
                        tick_size=_SALTO,
                        min_order_size=_MINIMO,
                    )
        raise AssertionError(f"libro de un resultado desconocido: {token_id}")

    # ------------------------------------------------------- escritura  #
    def build_order(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
    ) -> PredictionOrder:
        return PredictionOrder(
            venue_id=market.venue.venue_id,
            chain=market.venue.chain,
            market_id=market.market_id,
            condition_id=market.condition_id or "",
            question=market.question,
            outcome_label=outcome_label,
            token_id=market.outcome(outcome_label).token_id or "",  # type: ignore[union-attr]
            side=side,
            price=price,
            size=size,
            tick_size=market.tick_size or _SALTO,
            neg_risk=bool(market.neg_risk),
        )

    def sign_order(
        self, order: PredictionOrder, *, private_key: str, wallet: str | None = None
    ) -> Any:
        self.firmadas.append(order)
        raise AssertionError("el motor falso no firma: la prueba publica sin firmar")

    async def submit_order(self, signed: Any, *, private_key: str) -> Any:
        raise AssertionError("no se llega a publicar en esta prueba")

    def collateral_for(self, market: PredictionMarket) -> Token:
        return self.colateral

    def shares_collection_for(self, market: PredictionMarket) -> str:
        return "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    def exchange_for(self, market: PredictionMarket) -> str:
        return "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

    # ---- El canal de la deposit wallet: los métodos tienen que **existir** ----
    # Ninguna prueba de este fichero lo usa —aquí se opera por la EOA—, pero
    # `EngineRegistry.prediction_planner` comprueba el tipo en tiempo de
    # ejecución: al doble que le faltara uno de estos lo tomaría por un motor de
    # sólo lectura, y la tarjeta diría «no sabe operar» por el doble en vez de
    # por lo que se está midiendo.
    def settlement_wallet(self, owner: str) -> str:
        raise AssertionError("esta prueba opera por la EOA: no hay deposit wallet")

    async def deploy_settlement_wallet(self, *, owner: str, credentials: Any) -> str:
        raise AssertionError("esta prueba no despliega wallets")

    async def approve_wallet_collateral(
        self,
        *,
        owner: str,
        collateral: Token,
        spender: str,
        private_key: str,
        credentials: Any,
    ) -> str:
        raise AssertionError("esta prueba no concede permisos por el relayer")

    async def approve_wallet_shares(
        self,
        *,
        owner: str,
        collection: str,
        spender: str,
        private_key: str,
        credentials: Any,
    ) -> str:
        raise AssertionError("esta prueba no autoriza participaciones por el relayer")


class ProveedorFalso:
    def __init__(self) -> None:
        self.instancia = MotorFalso()

    @property
    def manifest(self) -> EngineManifest:
        return self.instancia.manifest

    def create(self, config: Mapping[str, str]) -> MotorFalso:
        return self.instancia


def _mercado() -> PredictionMarket:
    """Un mercado **operable**: con todo lo que hace falta para firmar una orden."""
    return PredictionMarket(
        market_id="operable",
        venue=VENUE,
        question="¿Se aprueba la propuesta?",
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.60"), token_id=str(next(_RESULTADOS))),
            MarketOutcome(label="No", price=Decimal("0.40"), token_id=str(next(_RESULTADOS))),
        ),
        observed_at=AHORA,
        closes_at=AHORA + timedelta(days=10),
        condition_id="0x" + "ab" * 32,
        neg_risk=False,
        tick_size=_SALTO,
        min_order_size=_MINIMO,
    )


def _mercado_a_medias() -> PredictionMarket:
    """Un mercado que se lee bien y **no se puede operar**: sin mínimo ni identificador."""
    return PredictionMarket(
        market_id="a-medias",
        venue=VENUE,
        question="¿Se aprueba la otra?",
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.30")),
            MarketOutcome(label="No", price=Decimal("0.70")),
        ),
        observed_at=AHORA,
        closes_at=AHORA + timedelta(days=3),
    )


_MERCADOS = (_mercado(), _mercado_a_medias())

#: Con esto la orden queda publicable salvo por lo que cada prueba quite.
_TERMINOS: dict[str, object] = {
    "enabled": True,
    "allowed_chains": ["polygon"],
    "allowed_tokens": ["USDC"],
    "allowed_engines": ["falso"],
}


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _store(*, con_cartera: bool) -> InMemorySecretStore:
    store = InMemorySecretStore()
    if con_cartera:
        store.set(app_secret_key(PRIVATE_KEY_SECRET), CLAVE_DE_DESARROLLO)
    return store


@asynccontextmanager
async def _pagina(
    *,
    modo: OperationMode = OperationMode.EXECUTION,
    con_cartera: bool = True,
    terminos: dict[str, object] | None = None,
) -> AsyncIterator[tuple[Container, PredictionPage, MotorFalso]]:
    proveedor = ProveedorFalso()
    config: dict[str, object] = {
        "mode": modo.value,
        "execution": _TERMINOS if terminos is None else terminos,
    }
    container = await build_container(
        Settings.model_validate(config),
        clock=FrozenClock(AHORA),
        secret_store=_store(con_cartera=con_cartera),
        discover=False,
        configure_logs=False,
    )
    try:
        container.registry.register(proveedor, source="prueba")
        await container.registry.activate("falso")
        yield container, PredictionPage(container), proveedor.instancia
    finally:
        await container.aclose()


async def _abrir(pagina: PredictionPage, fila: int = 0) -> None:
    """Busca, elige una fila y deja la tarjeta cargada con ese mercado.

    Se pulsa el botón de verdad y se espera a que vuelva, en vez de llamar a los
    métodos a mano: lo que se prueba es la cadena «buscar → seleccionar → cargar
    la tarjeta → leer el libro», y saltarse un eslabón dejaría fuera justo la
    parte que puede romperse.
    """
    pagina._on_search()
    while not pagina._btn.isEnabled():
        await _ceder()
    pagina._table.selectRow(fila)
    # La lectura del libro la lanza la selección en segundo plano.
    for _ in range(20):
        await _ceder()
    await _ceder()


async def _ceder() -> None:
    import asyncio

    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Que exista: la tarjeta se llena con el mercado elegido
# --------------------------------------------------------------------------- #
async def test_la_tarjeta_toma_las_reglas_del_mercado_elegido() -> None:
    """El mínimo de participaciones y el salto de precio son **de ese** mercado.

    Los publica la fuente y cambian de uno a otro. Un campo con los límites del
    mercado anterior deja escribir una orden que el recinto rechaza entera, y
    descubrirlo al firmar es descubrirlo después de haberla confirmado.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)

        assert [pagina._outcome.itemText(i) for i in range(pagina._outcome.count())] == [
            "Sí — 60.0 %",
            "No — 40.0 %",
        ]
        # El mínimo del mercado se aplica distinto por lado, porque cada lado se
        # escribe distinto: al comprar —en dinero— el campo admite cualquier
        # importe y el mínimo lo vigila el bloqueo con su motivo escrito; al
        # vender —en participaciones— el campo no baja de ahí.
        assert pagina._shares.minimum() == 0.01
        assert Decimal(str(pagina._amount.value())) == Decimal("5")
        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        assert pagina._shares.minimum() == float(_MINIMO)
        assert pagina._price.singleStep() == float(_SALTO)
        assert pagina._price.decimals() == 2
        assert pagina._price.minimum() == float(_SALTO)
        assert pagina._price.maximum() == float(Decimal(1) - _SALTO)
        assert "Se aprueba la propuesta" in pagina._order_market.text()


async def test_las_reglas_del_mercado_se_leen_en_las_dos_caras() -> None:
    """El mínimo y el salto, a la vista siempre —comprar y vender—.

    Antes sólo se nombraba el mínimo en la línea derivada de la compra, así que
    al vender no había forma de saberlo sin que algo fallara. La línea de reglas
    vive fuera de las dos caras justo por eso: el mínimo aplica igual a comprar
    y a vender, y el lado que no se está mirando no puede llevársela por
    delante.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)

        assert pagina._market_limits.text() == (
            "mínimo del mercado: 5 participaciones · salto de precio: 0.01"
        )
        # La línea no viaja con la cara de compra: al vender sigue ahí.
        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        assert pagina._market_limits.text() == (
            "mínimo del mercado: 5 participaciones · salto de precio: 0.01"
        )


async def test_un_mercado_sin_reglas_publicadas_lo_dice_en_vez_de_inventarlas() -> None:
    """Sin mínimo ni salto publicados, la línea dice que no constan.

    Los campos usan valores de socorro —1 y 0,01— para poder existir siquiera,
    pero enseñarlos aquí sería hacer pasar por regla del recinto lo que es una
    elección nuestra. Se dice lo que falta, que es lo mismo que bloquea el
    botón con su motivo escrito.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina, fila=1)

        assert pagina._market_limits.text() == (
            "no consta el mínimo de participaciones · no consta el salto de precio"
        )


async def test_volver_a_la_lista_vacia_las_reglas_del_mercado() -> None:
    """La tarjeta sobrevive a la tabla, así que las reglas tienen que irse con el mercado.

    Sin esto, una búsqueda nueva dejaría «mínimo del mercado: 5» bajo el cartel
    de «Selecciona un mercado de la lista»: la regla de un mercado que ya no
    está, leída como una regla vigente.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        assert pagina._market_limits.text() != ""

        pagina._clear_order_card()

        assert pagina._market_limits.text() == ""


async def test_el_precio_se_propone_desde_el_libro_no_desde_el_precio_publicado() -> None:
    """El precio publicado es una opinión; el libro es lo que se puede cruzar.

    Al comprar se propone el mejor **ask** y al vender el mejor **bid**, porque
    son los precios a los que de verdad hay contrapartida. Proponer el precio
    publicado —que es el último cruzado— dejaría una orden que no se cruza, y en
    una orden límite eso significa quedarse mirando.
    """
    async with _pagina() as (_, pagina, motor):
        await _abrir(pagina)

        assert pagina._side_value() is PredictionSide.BUY
        assert pagina._price.value() == pytest.approx(0.62)

        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        assert pagina._side_value() is PredictionSide.SELL
        assert pagina._price.value() == pytest.approx(0.58)
        assert motor.libros_pedidos, "el panel tiene que pedir el libro del resultado"


async def test_el_libro_enseña_si_el_tamaño_pedido_cabe() -> None:
    """Cruzar el libro y coger el mejor precio no son lo mismo, y se ve.

    Con dos niveles, comprar 15 participaciones cuesta 10 al primer precio y 5
    al segundo: la cifra que sale de recorrer la profundidad es distinta de la
    que sale de multiplicar por el mejor precio, y esa diferencia es el margen
    de la operación.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        # Comprar se escribe en dinero: 9,30 $ al ask propuesto —0,62— son 15
        # participaciones exactas.
        pagina._amount.setValue(9.30)

        texto = pagina._book_label.text()
        assert "compra 0.58" in texto
        assert "venta 0.62" in texto
        # 10 x 0.62 + 5 x 0.70 = 9.70, no 15 x 0.62 = 9.30.
        assert "9.70" in texto


async def test_un_tamaño_que_el_libro_no_tiene_se_dice_y_no_se_calcula() -> None:
    """«No cabe» y «cuesta esto» son respuestas distintas.

    Devolver el coste de la parte que sí cabe daría un número con pinta de
    precio y sin serlo, y sobre ese número se decide.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        # 310 $ a 0,62 son 500 participaciones, más de las que hay en venta.
        pagina._amount.setValue(310)

        assert "más de lo que hay en venta" in pagina._book_label.text()


async def test_el_coste_y_el_pago_se_enseñan_juntos() -> None:
    """El coste sin el pago potencial es la mitad de lo que hace falta decidir."""
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._price.setValue(0.60)
        # 6 $ a 0,60 son 10 participaciones exactas.
        pagina._amount.setValue(6)

        assert pagina._order_cost.text() == "Pagas 6.00 USDC"
        pago = pagina._order_payout.text()
        assert "10.0 participaciones" in pago
        assert "USDC si acierta el resultado" in pago

        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        assert pagina._order_cost.text().startswith("Cobras")


# --------------------------------------------------------------------------- #
# El botón que firma, y por qué está apagado
# --------------------------------------------------------------------------- #
async def test_con_todo_puesto_el_boton_de_publicar_se_enciende() -> None:
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        assert pagina._order_note.text() == ""
        assert pagina._submit_btn.isEnabled() is True


async def test_el_colateral_en_caja_mixta_no_apaga_el_boton() -> None:
    """`pUSD` con una lista que declara `PUSD` es un token permitido, no un bloqueo.

    El catálogo escribe el colateral del recinto con caja mixta —`pUSD`— y la
    lista llega normalizada a mayúsculas. Comparar en crudo apagaba el botón con
    «el colateral «pUSD» no está en `allowed_tokens` (ahora declara: PUSD)», que
    se lee como un error de la propia configuración cuando la configuración está
    bien: el bloqueo que costaba creer.
    """
    async with _pagina(terminos={**_TERMINOS, "allowed_tokens": ["PUSD"]}) as (
        _,
        pagina,
        motor,
    ):
        motor.colateral = Token(
            symbol="pUSD",
            decimals=6,
            chain="polygon",
            address="0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",
        )
        await _abrir(pagina)

        assert pagina._order_note.text() == ""
        assert pagina._submit_btn.isEnabled() is True


@pytest.mark.parametrize(
    ("cambio", "esperado"),
    [
        ({"modo": OperationMode.OBSERVATION}, "no permite emitir"),
        ({"terminos": {**_TERMINOS, "enabled": False}}, "enabled = true"),
        ({"terminos": {**_TERMINOS, "allowed_tokens": ["WETH"]}}, "allowed_tokens"),
        ({"terminos": {**_TERMINOS, "allowed_engines": ["otro"]}}, "allowed_engines"),
        ({"terminos": {**_TERMINOS, "allowed_chains": ["base"]}}, "allowed_chains"),
        ({"con_cartera": False}, "cartera"),
    ],
)
async def test_cada_pieza_que_falta_se_nombra_y_apaga_el_boton(
    cambio: dict[str, object], esperado: str
) -> None:
    """Se quita una pieza cada vez, para que se vea cuál produce qué motivo.

    Y se devuelven **todos** los motivos, no el primero: son condiciones
    distintas —el modo se cambia aquí, el interruptor y las listas en el fichero,
    la cartera aparte— y descubrirlas de una en una, arreglando una para que
    aparezca la siguiente, es lo que hace pensar que la aplicación está rota en
    vez de a medio configurar.
    """
    async with _pagina(**cambio) as (_, pagina, _):  # type: ignore[arg-type]
        await _abrir(pagina)

        assert pagina._submit_btn.isEnabled() is False
        nota = pagina._order_note.text()
        assert nota.startswith("No se puede publicar:")
        assert esperado in nota


async def test_el_comodin_de_redes_deja_publicar_en_cualquier_red() -> None:
    """`allowed_chains = ["*"]` es «cualquiera»: la red deja de ser un motivo.

    El contrapunto de la lista de piezas que faltan: la misma comprobación que
    bloqueaba con `["base"]` deja pasar con el comodín, porque la decisión pasa
    por `allows_chain` —la misma que usará el caso de uso al firmar— y no por
    una comparación en crudo que leería `"*"` como una red llamada asterisco.
    """
    async with _pagina(terminos={**_TERMINOS, "allowed_chains": ["*"]}) as (_, pagina, _):
        await _abrir(pagina)

        assert pagina._order_note.text() == ""
        assert pagina._submit_btn.isEnabled() is True


async def test_un_mercado_al_que_le_faltan_datos_se_dice_entero() -> None:
    """Un mercado leído sin los datos para firmar no lo arregla ninguna configuración.

    Sus motivos van **los primeros** de la lista, porque son los únicos que no
    dependen de nada que el usuario pueda tocar aquí.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina, fila=1)

        assert pagina._submit_btn.isEnabled() is False
        nota = pagina._order_note.text()
        assert "identificador del mercado" in nota
        assert "mínimo de participaciones" in nota


async def test_el_minimo_del_mercado_no_se_puede_escribir_por_debajo() -> None:
    """En la venta el campo no admite un número que el recinto rechazaría.

    Al comprar el mínimo lo vigila el bloqueo con su motivo escrito —el campo
    de importe admite cualquier cifra, porque recortarla sería comprar dinero
    que nadie escribió—; al vender el tamaño es lo que se escribe, y ahí el
    campo directamente no deja bajar del mínimo.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        pagina._shares.setValue(1)
        assert pagina._shares.value() == float(_MINIMO)


async def test_el_importe_se_convierte_en_participaciones_hacia_abajo() -> None:
    """Comprar se escribe en dinero y el tamaño sale de dividir por el precio.

    Hacia **abajo**: redondear hacia arriba daría participaciones que cuestan
    más de lo que el importe escrito paga, y el importe es lo que el usuario
    comprometió. La cifra que se firma —y que el coste enseña— son las
    participaciones derivadas, no el importe.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        assert pagina._price.value() == pytest.approx(0.62)

        pagina._amount.setValue(20)
        # 20 / 0,62 = 32,258… → 32,25. Las 32,25 cuestan 19,995; las 32,26
        # costarían 20,0012, por encima del importe escrito.
        assert Decimal(str(pagina._shares.value())) == Decimal("32.25")
        assert pagina._order_cost.text() == "Pagas 19.9950 USDC"
        # Y la derivación se enseña: es la respuesta a «¿cuántas me llevo?».
        # El mínimo no se repite aquí: vive en la línea de reglas del mercado,
        # que se lee de las dos caras.
        assert "32.25 participaciones" in pagina._buy_derived.text()
        assert "mínimo" not in pagina._buy_derived.text()


async def test_un_importe_que_no_llega_al_minimo_apaga_y_dice_por_que() -> None:
    """El campo de importe no se recorta solo: la orden se apaga y se explica.

    Subirle el importe al usuario sería comprar más dinero del que escribió, y
    las participaciones no se pueden subir sin eso. Así que queda apagada con
    el motivo escrito, que es lo que se puede arreglar.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._amount.setValue(1)

        # 1 $ a 0,62 cruza el libro (hay venta a ese precio), y el recinto
        # valida esa compra por **importe**: las 1,61 participaciones derivadas
        # valen 0,9982 $, por debajo del 1 $. El importe se queda tal como se
        # escribió y el motivo dice la cifra que sí llega: 1,01 $.
        assert Decimal(str(pagina._shares.value())) == Decimal("1.61")
        assert Decimal(str(pagina._amount.value())) == Decimal("1")
        assert pagina._submit_btn.isEnabled() is False
        assert "mínimo de 1 $ de importe" in pagina._order_note.text()
        assert "se queda en 0.9982 $" in pagina._order_note.text()
        assert "sube el importe a 1.01 $" in pagina._order_note.text()


async def test_una_compra_que_cruza_ya_no_la_para_el_minimo_de_participaciones() -> None:
    """La regla del recinto para una compra que cruza es el importe, no el tamaño.

    Medido: el recinto valida la compra que cruza por importe (≥ 1 $) y reserva
    el mínimo de participaciones para las órdenes que descansan. Con el tamaño
    aplicado a toda compra, este caso se apagaba aunque el recinto lo acepta:
    era el de «solo me deja operar 5 $».
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._amount.setValue(1.01)

        # 1,01 / 0,62 = 1,6290… → 1,62 participaciones, por debajo de las 5 del
        # mercado, y la orden sigue publicable: cruza y su importe real
        # —1,62 x 0,62 = 1,0044 $— pasa el 1 $ del recinto.
        assert Decimal(str(pagina._shares.value())) == Decimal("1.62")
        assert pagina._submit_btn.isEnabled() is True
        assert pagina._order_note.text() == ""


async def test_una_orden_que_descansa_sigue_el_minimo_de_participaciones() -> None:
    """El mínimo de 5 participaciones no desapareció: es el de las órdenes que descansan.

    A 0,50 —por debajo del mejor ask, 0,62— la compra no cruza: se queda en el
    libro esperando contrapartida. Ahí el recinto la valida por tamaño, y 1 $ a
    0,50 son 2 participaciones: se apaga con el motivo de siempre.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._price.setValue(0.50)
        pagina._amount.setValue(1)

        assert Decimal(str(pagina._shares.value())) == Decimal("2.00")
        assert pagina._submit_btn.isEnabled() is False
        assert "mínimo de 5 participaciones" in pagina._order_note.text()


async def test_los_atajos_de_compra_fijan_el_importe() -> None:
    """Los atajos escriben el campo, no sustituyen la lógica: el tamaño sale de él."""
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        atajos = {b.text(): b for b in pagina._buy_box.findChildren(QPushButton)}
        assert set(atajos) >= {"$1", "$5", "$20", "$100"}

        atajos["$20"].click()
        assert Decimal(str(pagina._amount.value())) == Decimal("20")
        assert Decimal(str(pagina._shares.value())) == Decimal("32.25")
        # El campo sigue siendo editable: el atajo es un atajo, no una jaula.
        assert pagina._amount.isReadOnly() is False


async def test_los_atajos_de_venta_fijan_las_participaciones() -> None:
    """Vender se escribe en participaciones, y los atajos son cifras redondas."""
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        pagina._side.setCurrentIndex(pagina._side.findData(PredictionSide.SELL))
        atajos = {b.text(): b for b in pagina._sell_box.findChildren(QPushButton)}
        assert set(atajos) >= {"5", "10", "25", "50", "100"}

        atajos["25"].click()
        assert Decimal(str(pagina._shares.value())) == Decimal("25")


# --------------------------------------------------------------------------- #
# Publicar
# --------------------------------------------------------------------------- #
class _PublicadorFalso:
    """Recoge con qué se le llamó, sin firmar ni salir a la red."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    async def __call__(
        self,
        market: PredictionMarket,
        *,
        outcome_label: str,
        side: PredictionSide,
        size: Decimal,
        price: Decimal,
        recipient: str,
    ) -> SubmittedPredictionOrder:
        self.llamadas.append(
            {
                "market": market,
                "outcome_label": outcome_label,
                "side": side,
                "size": size,
                "price": price,
                "recipient": recipient,
            }
        )
        return SubmittedPredictionOrder(order_id="orden-1", status="live")


async def test_publicar_llama_al_caso_de_uso_con_lo_que_dice_la_pantalla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lo que se firma es lo que se leyó, y el destinatario es la propia cartera.

    En un recinto de predicción la orden la hace quien la firma: no hay un
    destinatario distinto que elegir, y por eso el panel no lo pregunta —lo
    enseña—. Afirmar aquí sobre el destinatario es lo que impide que mañana
    alguien añada un campo «recibe» que el protocolo no tiene.
    """
    async with _pagina() as (container, pagina, _):
        publicador = _PublicadorFalso()
        monkeypatch.setattr(container, "place_prediction_order", publicador, raising=True)

        await _abrir(pagina)
        # Lo que se firma son las participaciones derivadas del importe, no el
        # importe: 7,44 $ a 0,62 son 12 exactas.
        pagina._amount.setValue(7.44)
        pagina._price.setValue(0.62)
        pagina._on_submit()
        for _ in range(20):
            await _ceder()

        assert len(publicador.llamadas) == 1
        llamada = publicador.llamadas[0]
        assert llamada["market"].market_id == "operable"
        assert llamada["outcome_label"] == "Sí"
        assert llamada["side"] is PredictionSide.BUY
        assert llamada["size"] == Decimal("12")
        assert llamada["price"] == Decimal("0.62")
        assert llamada["recipient"] == DIRECCION
        assert "orden-1" in pagina._order_status.text()
        assert "live" in pagina._order_status.text()


async def test_el_resultado_publicado_dice_que_no_es_una_transaccion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publicar no es emitir, y la pantalla no puede dejar creer lo contrario.

    Una transacción emitida no se puede tocar; esta orden está en un libro y se
    puede cancelar mientras nadie la haya cruzado. La diferencia decide si hay
    algo que hacer cuando algo sale mal.
    """
    async with _pagina() as (container, pagina, _):
        monkeypatch.setattr(
            container, "place_prediction_order", _PublicadorFalso(), raising=True
        )
        await _abrir(pagina)
        pagina._on_submit()
        for _ in range(20):
            await _ceder()

        assert "no es una transacción" in pagina._order_status.text().lower()


async def test_si_el_caso_de_uso_falla_el_motivo_se_lee_en_la_tarjeta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un fallo del caso de uso no puede quedar sólo en el log."""

    class _Explota:
        async def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("el recinto rechazó la firma")

    async with _pagina() as (container, pagina, _):
        monkeypatch.setattr(container, "place_prediction_order", _Explota(), raising=True)
        await _abrir(pagina)
        pagina._on_submit()
        for _ in range(20):
            await _ceder()

        assert "el recinto rechazó la firma" in pagina._order_status.text()
        # Y el botón vuelve a estar disponible: un fallo no es un estado final.
        assert pagina._submit_btn.isEnabled() is True


# --------------------------------------------------------------------------- #
# La tarjeta no puede sobrevivir al mercado que describe
# --------------------------------------------------------------------------- #
async def test_una_busqueda_nueva_vacia_la_tarjeta() -> None:
    """La tarjeta sobrevive a la tabla, y por eso hay que vaciarla a mano.

    Cuando una búsqueda reemplaza las filas, la selección desaparece y la
    tarjeta se quedaría enseñando el mercado anterior con sus precios y su libro
    como si siguiera elegido. Operar sobre lo que la tabla ya no muestra es la
    forma más silenciosa de firmar lo que no se está mirando.
    """
    async with _pagina() as (_, pagina, _):
        await _abrir(pagina)
        assert pagina._outcome.count() == 2

        pagina._on_search()
        while not pagina._btn.isEnabled():
            await _ceder()

        assert pagina._outcome.count() == 0
        assert pagina._submit_btn.isEnabled() is False
        assert pagina._order_cost.text() == "—"
        # Y sin mercado no se avisa de nada: antes de elegir, «no se puede
        # publicar» no dice nada que el usuario no sepa ya.
        assert pagina._order_note.text() == ""
        assert "Selecciona un mercado" in pagina._order_market.text()


# --------------------------------------------------------------------------- #
# El redondeo al salto del mercado
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("precio", "esperado"),
    [
        (Decimal("0.62"), Decimal("0.62")),
        (Decimal("0.6237"), Decimal("0.62")),
        (Decimal("0.0099"), Decimal("0.00")),
        (Decimal("0.999"), Decimal("0.99")),
    ],
)
def test_el_precio_se_baja_al_multiplo_del_salto(precio: Decimal, esperado: Decimal) -> None:
    """Hacia **abajo**, y la dirección es deliberada.

    El precio propuesto sale de uno ya observado en el libro, y redondearlo hacia
    arriba propondría pagar más de lo que el mercado pide. Bajando, el número
    propuesto nunca es peor que el observado; subiendo, siempre lo es en la
    mitad de los casos.
    """
    assert _round_to_tick(precio, _SALTO) == esperado
