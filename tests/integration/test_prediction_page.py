"""El selector de ventana de la pestaña de predicción: qué enseña y qué no pide.

Lo que se fija aquí es una decisión de producto que es fácil de equivocar sin
que nada falle: **cambiar la ventana no vuelve a salir a la red**. Mover el
selector tiene que ser instantáneo y reversible —se puede volver a «todas» y
recuperar la lista entera—, y si cada movimiento pidiera datos, la pestaña
golpearía una API pública ajena cada vez que alguien prueba las cuatro opciones.

Y lo contrario, que es lo que sí tiene que salir a la red: la **búsqueda** lleva
la ventana al motor, porque recortar en cliente lo ya traído no puede añadir los
mercados que cierran pronto y no estaban entre los treinta de más volumen. Las
dos mitades se comprueban contando peticiones.

El motor está falseado —devuelve mercados con fechas conocidas y cuenta las
llamadas— y todo lo demás es real: el `Container`, el registro, el caso de uso y
los widgets de Qt. Doblar la vista aquí no serviría, porque lo que se prueba es
justamente lo que la vista le pasa al motor.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem

from amigocompora.app.container import build_container
from amigocompora.app.usecases.analyze_prediction_market import MarketReport
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    DepthLevel,
    MarketDepth,
    MarketOutcome,
    PredictionMarket,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import BasisPoints
from amigocompora.domain.protocols import EngineKind, EngineManifest
from amigocompora.infra.config import Settings
from amigocompora.ui.pages.prediction import PredictionPage

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

VENUE = Venue(
    venue_id="falso", name="Falso", kind=VenueKind.PREDICTION_MARKET, chain="polygon"
)

MANIFEST = EngineManifest(
    engine_id="falso",
    name="Motor falso",
    version="1.0.0",
    kind=EngineKind.PREDICTION_MARKETS,
    summary="Devuelve mercados con fechas conocidas y cuenta las peticiones.",
    capabilities=frozenset(),
    required_config=(),
    allowed_hosts=(),
)


class MotorFalso:
    """Mercados con fechas fijas, y un contador de cuántas veces se le pidió."""

    def __init__(self) -> None:
        self.llamadas: list[dict[str, object]] = []
        #: Puesto a `True`, la fuente falla. Sirve para comprobar que una
        #: búsqueda que **no llega a hacerse** no se cuenta igual que una que se
        #: hizo y no encontró nada.
        self.falla = False

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None: ...
    async def aclose(self) -> None: ...

    async def markets(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        closing_within: timedelta | None = None,
    ) -> Sequence[PredictionMarket]:
        self.llamadas.append(
            {"limit": limit, "search": search, "closing_within": closing_within}
        )
        if self.falla:
            raise RuntimeError("la fuente no contesta")
        encontrados = [m for m in _MERCADOS if search is None or search in m.question]
        if closing_within is not None:
            # Como haría el motor de verdad: lo que antes cierra, primero, y sin
            # fecha o ya cerrado fuera.
            vivos = [
                (m.closes_at, m)
                for m in encontrados
                if m.closes_at is not None and m.closes_at > AHORA
            ]
            vivos.sort(key=lambda par: par[0])
            encontrados = [m for _, m in vivos]
        return tuple(encontrados[:limit])

    async def market(self, market_id: str) -> PredictionMarket:
        for market in _MERCADOS:
            if market.market_id == market_id:
                return market
        raise AssertionError(market_id)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        """El mercado de una posición, por su `conditionId`.

        Los mercados de este doble no traen `conditionId`: la pestaña que esta
        prueba maneja no opera sobre posiciones de la wallet, y por eso el doble
        sólo declara que sabe hacerlo —lo exige el protocolo— sin tener ninguno
        que devolver.
        """
        for market in _MERCADOS:
            if market.condition_id == condition_id:
                return market
        raise AssertionError(condition_id)

    async def book(self, token_id: str) -> MarketDepth:
        """Un libro de un solo nivel por lado, para los resultados que conoce.

        Hace falta porque `PredictionMarketEngine.book` está en el protocolo de
        lectura: un motor de predicción que no sepa decir el libro de un
        resultado no se reconoce como motor de predicción. Es el precio que
        documenta el propio protocolo, y por eso el doble lo implementa en vez
        de esquivarlo.

        Un nivel por lado es suficiente aquí —el libro no es lo que esta prueba
        mide— pero es un libro **coherente**: la compra por debajo del precio
        publicado y la venta por encima.
        """
        for market in _MERCADOS:
            for outcome in market.outcomes:
                if outcome.token_id == token_id:
                    return MarketDepth(
                        token_id=token_id,
                        bids=(
                            DepthLevel(
                                price=outcome.price - _SALTO, size=Decimal("500")
                            ),
                        ),
                        asks=(
                            DepthLevel(
                                price=outcome.price + _SALTO, size=Decimal("500")
                            ),
                        ),
                        tick_size=_SALTO,
                        min_order_size=Decimal("5"),
                    )
        raise AssertionError(token_id)


def _mercado(market_id: str, *, horas: float | None, pregunta: str) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        venue=VENUE,
        question=pregunta,
        outcomes=(
            MarketOutcome(
                label="Sí", price=Decimal("0.6"), token_id=str(next(_TOKEN))
            ),
            MarketOutcome(
                label="No", price=Decimal("0.4"), token_id=str(next(_TOKEN))
            ),
        ),
        observed_at=AHORA,
        closes_at=None if horas is None else AHORA + timedelta(hours=horas),
    )


#: Identificadores de resultado, correlativos y únicos: el `tokenId` de un
#: ERC-1155 es un `uint256` en decimal, y el dominio lo exige así.
_TOKEN = count(1000)

#: El salto de precio de estos mercados, para que el libro del doble sea válido.
_SALTO = Decimal("0.01")


#: Cuatro mercados que caen uno en cada tramo del selector, más uno sin fecha.
_MERCADOS = (
    _mercado("2h", horas=2, pregunta="¿Ocurre en dos horas?"),
    _mercado("3d", horas=72, pregunta="¿Ocurre en tres días?"),
    _mercado("20d", horas=480, pregunta="¿Ocurre en veinte días?"),
    _mercado("90d", horas=2160, pregunta="¿Ocurre en noventa días?"),
    _mercado("sin", horas=None, pregunta="¿Ocurre algún día?"),
)


class ProveedorFalso:
    manifest = MANIFEST

    def __init__(self) -> None:
        self.instancia = MotorFalso()

    def create(self, config: Mapping[str, str]) -> MotorFalso:
        return self.instancia


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    """Un `QApplication` para todo el módulo: Qt no admite más de uno por proceso."""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@asynccontextmanager
async def _pagina() -> AsyncIterator[tuple[PredictionPage, MotorFalso]]:
    """La pestaña montada sobre un contenedor real, y su motor para espiarlo."""
    proveedor = ProveedorFalso()

    # Sin `discover`: los motores instalados de verdad no se cargan, y el único
    # activo en la ranura de predicción es el falso. El contenedor, el registro y
    # el caso de uso son los de producción.
    container = await build_container(
        Settings.model_validate({"mode": "observation"}),
        clock=FrozenClock(AHORA),
        discover=False,
        configure_logs=False,
    )
    try:
        container.registry.register(proveedor, source="prueba")
        await container.registry.activate("falso")
        yield PredictionPage(container), proveedor.instancia
    finally:
        await container.aclose()


async def _buscar(pagina: PredictionPage) -> None:
    """Pulsa Buscar y espera a que termine, que es lo que hace `spawn` en Qt."""
    pagina._on_search()
    while not pagina._btn.isEnabled():
        await _ceder()


async def _ceder() -> None:
    import asyncio

    await asyncio.sleep(0)


def _questions(pagina: PredictionPage) -> list[str]:
    tabla = pagina._table
    return [_celda(pagina, row, 0).text() for row in range(tabla.rowCount())]


def _select(pagina: PredictionPage, etiqueta: str) -> None:
    combo = pagina._window
    indice = combo.findText(etiqueta)
    assert indice >= 0, etiqueta
    combo.setCurrentIndex(indice)


def _celda(pagina: PredictionPage, row: int, column: int) -> QTableWidgetItem:
    """La celda, exigiendo que exista.

    `QTableWidget.item` devuelve `None` para una celda vacía, y una celda que no
    se pintó es justo lo que estas pruebas buscan: sin la aserción, un fallo de
    pintado se leería como un `AttributeError` sobre `None` y no como «esta celda
    no se llenó».
    """
    item = pagina._table.item(row, column)
    assert item is not None, f"celda vacía en fila {row}, columna {column}"
    return item


# --------------------------------------------------------------------------- #
# La cuenta atrás
# --------------------------------------------------------------------------- #
async def test_the_close_column_is_a_countdown() -> None:
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        celdas = [_celda(pagina, row, 5).text() for row in range(pagina._table.rowCount())]
        assert "en 2 h" in celdas
        assert "en 3 días" in celdas
        # El mercado sin fecha no se inventa un tiempo.
        assert "—" in celdas


async def test_the_exact_date_stays_available() -> None:
    """La cuenta atrás es para leer; el dato no se esconde."""
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        for row in range(pagina._table.rowCount()):
            assert _celda(pagina, row, 5).toolTip()


# --------------------------------------------------------------------------- #
# La ventana
# --------------------------------------------------------------------------- #
async def test_the_window_shortens_the_list() -> None:
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        assert len(_questions(pagina)) == len(_MERCADOS)

        _select(pagina, "24 h")
        assert _questions(pagina) == ["¿Ocurre en dos horas?"]

        _select(pagina, "7 días")
        assert _questions(pagina) == ["¿Ocurre en dos horas?", "¿Ocurre en tres días?"]

        _select(pagina, "30 días")
        assert _questions(pagina) == [
            "¿Ocurre en dos horas?",
            "¿Ocurre en tres días?",
            "¿Ocurre en veinte días?",
        ]


async def test_the_window_leaves_out_what_has_no_close_date() -> None:
    """Es la promesa de la vista: lo que está terminando. Sin fecha, no consta."""
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        _select(pagina, "24 h")
        assert "¿Ocurre algún día?" not in _questions(pagina)


async def test_going_back_to_all_restores_the_whole_list() -> None:
    """Reversible sin coste: el selector no es una búsqueda irreversible."""
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        _select(pagina, "24 h")
        _select(pagina, "todas")
        assert len(_questions(pagina)) == len(_MERCADOS)


async def test_changing_the_window_does_not_ask_the_source_again() -> None:
    """El punto de todo esto: mover el selector es gratis y es instantáneo."""
    async with _pagina() as (pagina, motor):
        await _buscar(pagina)
        assert len(motor.llamadas) == 1
        for etiqueta in ("24 h", "7 días", "30 días", "todas", "24 h"):
            _select(pagina, etiqueta)
        assert len(motor.llamadas) == 1


async def test_a_search_carries_the_window_to_the_engine() -> None:
    """La otra mitad: sin esto, «24 h» sólo recortaría lo que ya no basta.

    Con la ventana en la petición, el motor ordena por el reloj y filtra en el
    servidor, así que los mercados que se traen son los que cierran pronto. Si la
    ventana sólo se aplicara en cliente, una lista de treinta mercados de mucho
    volumen podría no traer ninguno que cierre mañana, y la pestaña diría que no
    hay nada cuando sí lo hay.
    """
    async with _pagina() as (pagina, motor):
        _select(pagina, "7 días")
        await _buscar(pagina)
        assert motor.llamadas[-1]["closing_within"] == timedelta(days=7)

        _select(pagina, "todas")
        await _buscar(pagina)
        assert motor.llamadas[-1]["closing_within"] is None


async def test_the_selection_still_points_at_the_visible_market() -> None:
    """Filtrar y luego elegir una fila no puede hablar de otro mercado.

    El detalle de abajo se resuelve por el índice de la fila contra la lista de
    informes. Si esa lista siguiera siendo la de antes de filtrar, elegir la
    primera fila visible contaría las probabilidades del primer mercado traído,
    que puede no estar ni en pantalla.
    """
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        _select(pagina, "7 días")
        pagina._table.selectRow(0)
        # La fila visible y el informe que el detalle va a leer son el mismo.
        assert pagina._reports[0].question == _questions(pagina)[0]


# --------------------------------------------------------------------------- #
# Las cestas: por qué no hay ninguna
# --------------------------------------------------------------------------- #
async def test_sin_buscar_las_cestas_dicen_que_falta_buscar() -> None:
    """Un rectángulo gris no distingue «no has buscado» de «no hay ninguna».

    Son dos cosas distintas y se arreglan de forma distinta: la primera se
    arregla pulsando Buscar, la segunda bajando el umbral o cambiando de
    mercado. La tabla vacía no decía cuál de las dos era.
    """
    async with _pagina() as (pagina, _):
        assert pagina._baskets.rowCount() == 0
        # `isHidden()` y no `isVisible()`: la ventana no se ha enseñado nunca, y
        # en Qt todo lo que cuelga de una ventana sin enseñar «no es visible».
        assert pagina._baskets.isHidden() is True
        assert pagina._baskets_empty.isHidden() is False
        assert "Busca mercados" in pagina._baskets_empty.text()


async def test_una_cesta_que_no_llega_al_umbral_se_explica_con_el_umbral() -> None:
    """Y el motivo nombra el umbral con su número, que es lo único que se mueve.

    Decir «ninguna llega al umbral» sin decir cuál obliga a adivinar cuánto
    bajarlo. El número está en la pantalla, al lado, en el selector: el texto
    lo repite para que las dos cosas se lean juntas.
    """
    async with _pagina() as (pagina, _):
        await _buscar(pagina)

        # Los mercados de prueba suman 1,00 exacto: ninguna cesta tiene margen.
        assert pagina._baskets.rowCount() == 0
        assert pagina._baskets.isHidden() is True
        assert pagina._baskets_empty.isHidden() is False
        motivo = pagina._baskets_empty.text()
        assert str(pagina._min_edge.value()) in motivo
        assert str(len(pagina._reports)) in motivo
        assert "Baja el umbral" in motivo


async def test_con_una_cesta_la_tabla_vuelve_y_el_rotulo_se_va() -> None:
    """Cuando sí hay margen, manda la tabla: las dos cosas a la vez, nunca."""
    async with _pagina() as (pagina, _):
        await _buscar(pagina)

        # Un mercado que se deja dinero sobre la mesa: 0,50 + 0,40 = 0,90.
        barato = PredictionMarket(
            market_id="barato",
            venue=VENUE,
            question="¿Se deja dinero sobre la mesa?",
            outcomes=(
                MarketOutcome(label="Sí", price=Decimal("0.50"), token_id=str(next(_TOKEN))),
                MarketOutcome(label="No", price=Decimal("0.40"), token_id=str(next(_TOKEN))),
            ),
            observed_at=AHORA,
            closes_at=None,
        )
        pagina._reports = (
            MarketReport(
                market=barato,
                overround_bps=BasisPoints(-1000),
                is_coherent=False,
                favourite=barato.outcomes[0],
            ),
        )
        pagina._refresh_baskets()

        assert pagina._baskets.rowCount() == 1
        assert pagina._baskets.isHidden() is False
        assert pagina._baskets_empty.isHidden() is True
        fila = pagina._baskets.item(0, 0)
        assert fila is not None
        assert fila.text() == "¿Se deja dinero sobre la mesa?"


async def test_al_abrir_la_pestana_la_tabla_dice_que_falta_buscar() -> None:
    """Sin esto quedaba un rectángulo gris con encabezados y nada dentro.

    Es la primera pantalla que se ve al entrar, y no distinguía «todavía no has
    pedido nada» de «pediste y no hay»: las dos se veían igual, y una de ellas
    tiene una acción evidente (pulsar Buscar) que la otra no.
    """
    async with _pagina() as (pagina, _):
        assert pagina._table.rowCount() == 0
        assert pagina._table.isHidden() is True
        assert pagina._markets_empty.isHidden() is False
        assert "Buscar" in pagina._markets_empty.text()


async def test_una_busqueda_sin_resultados_no_se_lee_como_un_fallo() -> None:
    """«No hay mercados para eso» y «la consulta falló» no son lo mismo.

    Las dos dejan la tabla vacía, y la acción que toca es distinta en cada caso:
    cambiar la palabra, o mirar el error. Compartir un solo texto obligaría a
    adivinar cuál de las dos ha pasado.
    """
    async with _pagina() as (pagina, motor):
        motor.falla = True
        await _buscar(pagina)

        assert pagina._status.text().startswith("Error:")
        motivo = pagina._markets_empty.text()
        assert "no llegó a completarse" in motivo
        assert "la línea de arriba" in motivo

        motor.falla = False
        await _buscar(pagina)
        assert pagina._markets_empty.isHidden() is True


async def test_la_ventana_que_deja_la_lista_vacia_lo_dice_y_ofrece_la_salida() -> None:
    """Un filtro que esconde todo tiene que decir cuál es, no parecer un fallo."""
    async with _pagina() as (pagina, _):
        await _buscar(pagina)
        # Una ventana en la que no cae ningún mercado de los de prueba.
        pagina._window.addItem("en nada", timedelta(seconds=1))
        _select(pagina, "en nada")

        assert pagina._table.rowCount() == 0
        motivo = pagina._markets_empty.text()
        assert str(len(pagina._all_reports)) in motivo
        assert "todas" in motivo
