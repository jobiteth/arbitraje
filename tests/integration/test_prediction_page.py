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

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem

from amigocompora.app.container import build_container
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    MarketOutcome,
    PredictionMarket,
    Venue,
    VenueKind,
)
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


def _mercado(market_id: str, *, horas: float | None, pregunta: str) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        venue=VENUE,
        question=pregunta,
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.6")),
            MarketOutcome(label="No", price=Decimal("0.4")),
        ),
        observed_at=AHORA,
        closes_at=None if horas is None else AHORA + timedelta(hours=horas),
    )


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
