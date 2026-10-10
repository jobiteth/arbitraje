"""Las oportunidades de compra y el catálogo: dos lecturas del mismo análisis.

Aritmética pura sobre objetos del dominio: sin red, sin motores reales, sin
reloj real. Lo que se fija aquí es la condición de arbitraje —una cesta que
cuesta menos de lo que paga— y que un mercado que cobra margen no se confunda
con ella.

La otra mitad fija el **catálogo**: el orden con el que se enseña la lista —por
omisión lo incoherente primero, y los cuatro criterios del selector— y el filtro
por categoría. Los dos viven como funciones puras porque la interfaz los vuelve
a aplicar sobre lo ya traído, sin salir a la red: tenerlos en un solo sitio es
lo que evita que el mismo selector ordene de dos maneras según de dónde venga la
lista. Y el caso de uso, cuando se le pide otro orden o una categoría, los
reenvía al motor sin cambiarlos por el camino.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.analyze_prediction_market import (
    AnalyzePredictionMarkets,
    MarketReport,
    filter_reports_by_tag,
    sort_reports,
)
from amigocompora.app.usecases.find_prediction_opportunities import (
    DEFAULT_MIN_EDGE_BPS,
    FindPredictionOpportunities,
)
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    MarketDepth,
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionSort,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints
from amigocompora.domain.protocols import EngineKind, EngineManifest

OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

VENUE = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain="polygon",
)

POLITICA = MarketTag(tag_id="2", label="Politics", slug="politics")
CRIPTO = MarketTag(tag_id="21", label="Crypto", slug="crypto")


def _market(
    *prices: str,
    question: str = "¿Ocurrirá?",
    market_id: str = "m1",
    closes_at: datetime | None = None,
    created_at: datetime | None = None,
    volume_24h: Decimal | None = None,
    tags: tuple[MarketTag, ...] = (),
) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        venue=VENUE,
        question=question,
        outcomes=tuple(
            MarketOutcome(label=f"Resultado {i}", price=Decimal(price))
            for i, price in enumerate(prices)
        ),
        observed_at=OBSERVED_AT,
        closes_at=closes_at,
        created_at=created_at,
        volume_24h=volume_24h,
        tags=tags,
    )


def _report(market: PredictionMarket) -> MarketReport:
    return MarketReport(
        market=market,
        overround_bps=market.overround_bps,
        is_coherent=market.is_coherent(),
        favourite=market.favourite,
    )


def _reports(*mercados: PredictionMarket) -> tuple[MarketReport, ...]:
    return tuple(_report(mercado) for mercado in mercados)


def _usecase() -> FindPredictionOpportunities:
    guard = ModeGuard(OperationMode.SIMULATION)
    gateway = ConfirmationGateway(guard)
    return FindPredictionOpportunities(
        analyze=AnalyzePredictionMarkets(
            registry=EngineRegistry(guard),
            gateway=gateway,
        ),
        gateway=gateway,
        clock=FrozenClock(OBSERVED_AT),
    )


def test_prices_summing_to_one_yield_nothing() -> None:
    """Un mercado justo no es una oportunidad: cuesta 1 y paga 1."""
    found = _usecase().from_reports((_report(_market("0.5", "0.5")),))
    assert found == ()


def test_market_charging_margin_is_not_an_opportunity() -> None:
    """Con overround positivo la cesta cuesta *más* que el pago.

    Es el caso contrario y el más frecuente: el mercado cobra margen. No puede
    colarse como oportunidad por un signo mal puesto.
    """
    found = _usecase().from_reports((_report(_market("0.55", "0.55")),))
    assert found == ()


def test_detects_basket_cheaper_than_its_payout() -> None:
    """0,48 + 0,48 = 0,96 por un pago garantizado de 1,00."""
    found = _usecase().from_reports((_report(_market("0.48", "0.48")),))
    assert len(found) == 1
    opportunity = found[0]
    assert opportunity.cost == Decimal("0.96")
    assert opportunity.profit_per_share == Decimal("0.04")
    assert opportunity.discount_bps == BasisPoints(400)
    assert opportunity.outcome_count == 2


def test_return_on_cost_is_higher_than_the_discount() -> None:
    """El capital desembolsado es 0,96, no 1: 0,04/0,96 = 416,66 bps."""
    found = _usecase().from_reports((_report(_market("0.48", "0.48")),))
    assert found[0].discount_bps == BasisPoints(400)
    assert found[0].return_on_cost_bps == BasisPoints(417)
    assert found[0].return_on_cost_bps.value > found[0].discount_bps.value


def test_multi_outcome_basket_is_summed_over_every_outcome() -> None:
    """Con cuatro resultados, la cesta son las cuatro patas, no dos."""
    found = _usecase().from_reports((_report(_market("0.20", "0.20", "0.20", "0.20")),))
    assert len(found) == 1
    assert found[0].outcome_count == 4
    assert found[0].cost == Decimal("0.80")
    assert found[0].discount_bps == BasisPoints(2_000)


def test_default_threshold_filters_noise() -> None:
    """0,499 + 0,499 son 20 bps: por debajo del mínimo por defecto, ruido."""
    report = _report(_market("0.499", "0.499"))
    usecase = _usecase()
    assert usecase.from_reports((report,)) == ()
    # Y con un umbral explícito más fino, aparece.
    found = usecase.from_reports((report,), min_edge_bps=BasisPoints(10))
    assert len(found) == 1
    assert found[0].discount_bps == BasisPoints(20)


def test_threshold_boundary_is_inclusive() -> None:
    """Justo en el umbral entra: el mínimo es «al menos esto», no «más que esto»."""
    found = _usecase().from_reports(
        (_report(_market("0.495", "0.495")),),
        min_edge_bps=BasisPoints(100),
    )
    assert len(found) == 1
    assert found[0].discount_bps == BasisPoints(100)


def test_sorted_by_discount_descending() -> None:
    reports = (
        _report(_market("0.48", "0.48", question="pequeña")),
        _report(_market("0.40", "0.40", question="grande")),
    )
    found = _usecase().from_reports(reports)
    assert [opp.question for opp in found] == ["grande", "pequeña"]


def test_default_threshold_matches_documented_value() -> None:
    assert BasisPoints(50) == DEFAULT_MIN_EDGE_BPS


def test_observed_at_comes_from_the_clock() -> None:
    found = _usecase().from_reports((_report(_market("0.48", "0.48")),))
    assert found[0].observed_at == OBSERVED_AT


# --------------------------------------------------------------------------- #
# El caso de uso: lo que se pide al motor es lo que se pidió
# --------------------------------------------------------------------------- #
class MotorFalso:
    """Un motor de predicción que anota la petición y devuelve lo que se le dio.

    No filtra ni ordena: aquí no se prueba el motor —eso es
    `test_polymarket_window`— sino que el caso de uso le **pase** el orden y la
    categoría tal cual los recibió.
    """

    manifest = EngineManifest(
        engine_id="falso",
        name="Falso",
        version="1.0.0",
        kind=EngineKind.PREDICTION_MARKETS,
        summary="Anota la petición y devuelve la lista dada.",
        capabilities=frozenset(),
        required_config=(),
        allowed_hosts=(),
    )

    def __init__(self, mercados: Sequence[PredictionMarket] = ()) -> None:
        self.mercados = mercados
        self.pedidos: list[dict[str, Any]] = []

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
        self.pedidos.append(
            {
                "limit": limit,
                "search": search,
                "closing_within": closing_within,
                "category": category,
                "sort": sort,
            }
        )
        return self.mercados

    async def market(self, market_id: str) -> PredictionMarket:
        raise AssertionError(market_id)

    async def market_by_condition(self, condition_id: str) -> PredictionMarket:
        raise AssertionError(condition_id)

    async def book(self, token_id: str) -> MarketDepth:
        raise AssertionError(token_id)


class _Provider:
    """Envuelve un motor para que el registro lo pueda abrir."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def manifest(self) -> EngineManifest:
        manifest: EngineManifest = self.engine.manifest
        return manifest

    def create(self, config: Mapping[str, str]) -> Any:
        del config
        return self.engine


async def _analizar(motor: MotorFalso, **llamada: Any) -> tuple[MarketReport, ...]:
    """El caso de uso sobre el registro, con el motor falseado."""
    guard = ModeGuard(OperationMode.SIMULATION)
    gateway = ConfirmationGateway(guard)
    registry = EngineRegistry(guard)
    registry.register(_Provider(motor), source="prueba")
    await registry.activate(motor.manifest.engine_id)
    analyze = AnalyzePredictionMarkets(registry=registry, gateway=gateway)
    return await analyze(**llamada)


async def test_the_default_order_is_coherence_and_no_category() -> None:
    """El defecto no cambia: lo incoherente primero, sin filtrar por categoría.

    `FindPredictionOpportunities` se apoya en ese orden, así que cambiarlo aquí
    cambiaría su comportamiento sin que él lo sepa.
    """
    motor = MotorFalso(
        (
            _market("0.48", "0.48", market_id="incoherente"),
            _market("0.5", "0.5", market_id="justo"),
        )
    )
    reports = await _analizar(motor)
    (pedido,) = motor.pedidos
    assert pedido["sort"] is PredictionSort.COHERENCE
    assert pedido["category"] is None
    assert [report.market.market_id for report in reports] == ["incoherente", "justo"]


async def test_the_order_and_the_category_travel_to_the_engine() -> None:
    """Lo que el usuario elige en los combos llega al motor sin traducirse."""
    motor = MotorFalso()
    await _analizar(motor, sort=PredictionSort.NEWEST, category=CRIPTO)
    (pedido,) = motor.pedidos
    assert pedido["sort"] is PredictionSort.NEWEST
    assert pedido["category"] is CRIPTO


# --------------------------------------------------------------------------- #
# El orden, como función pura: la interfaz lo reaplica sin salir a la red
# --------------------------------------------------------------------------- #
def test_newest_puts_the_newest_first_and_the_undated_last() -> None:
    """Sin fecha conocida, al final: no consta que sea lo más nuevo."""
    viejo = _market("0.5", "0.5", market_id="viejo", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    nuevo = _market("0.5", "0.5", market_id="nuevo", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    sin_fecha = _market("0.5", "0.5", market_id="sin_fecha")
    ordered = sort_reports(
        _reports(viejo, sin_fecha, nuevo), PredictionSort.NEWEST
    )
    assert [report.market.market_id for report in ordered] == ["nuevo", "viejo", "sin_fecha"]


def test_trending_puts_what_moves_now_first_and_the_undated_last() -> None:
    parado = _market("0.5", "0.5", market_id="parado", volume_24h=Decimal("10"))
    movido = _market("0.5", "0.5", market_id="movido", volume_24h=Decimal("900"))
    sin_dato = _market("0.5", "0.5", market_id="sin_dato")
    ordered = sort_reports(
        _reports(parado, sin_dato, movido), PredictionSort.TRENDING
    )
    assert [report.market.market_id for report in ordered] == ["movido", "parado", "sin_dato"]


def test_volume_keeps_the_order_the_source_gave() -> None:
    """Ese orden ya lo puso la fuente al pedirle lo más negociado.

    Reordenar aquí por un dato que puede faltar cambiaría un orden real por una
    aproximación, y la lista dejaría de ser la que la fuente publicó.
    """
    poco = _market("0.5", "0.5", market_id="poco", volume_24h=Decimal("1"))
    mucho = _market("0.5", "0.5", market_id="mucho", volume_24h=Decimal("9"))
    ordered = sort_reports(_reports(poco, mucho), PredictionSort.VOLUME)
    assert [report.market.market_id for report in ordered] == ["poco", "mucho"]


def test_coherence_puts_the_most_incoherent_first() -> None:
    """Es el defecto: la señal que el usuario busca va arriba."""
    justo = _market("0.5", "0.5", market_id="justo")
    poco = _market("0.49", "0.49", market_id="poco")
    mucho = _market("0.45", "0.45", market_id="mucho")
    ordered = sort_reports(
        _reports(justo, poco, mucho), PredictionSort.COHERENCE
    )
    assert [report.market.market_id for report in ordered] == ["mucho", "poco", "justo"]


def test_the_window_sorts_by_the_clock_over_the_chosen_order() -> None:
    """La vista de lo que está terminando es del reloj, no del selector."""
    tarde = _market("0.45", "0.45", market_id="tarde", closes_at=datetime(2026, 7, 1, tzinfo=UTC))
    pronto = _market("0.5", "0.5", market_id="pronto", closes_at=datetime(2026, 2, 1, tzinfo=UTC))
    sin_fecha = _market("0.5", "0.5", market_id="sin_fecha")
    ordered = sort_reports(
        _reports(tarde, sin_fecha, pronto),
        PredictionSort.NEWEST,
        closing_within=timedelta(days=30),
    )
    assert [report.market.market_id for report in ordered] == ["pronto", "tarde", "sin_fecha"]


def test_the_category_filter_keeps_the_tagged_and_the_untagged() -> None:
    """Sólo se descarta a quien consta que no la lleva: sin etiquetas, se queda."""
    con = _market("0.5", "0.5", market_id="con", tags=(POLITICA,))
    sin = _market("0.5", "0.5", market_id="sin")
    otra = _market("0.5", "0.5", market_id="otra", tags=(CRIPTO,))
    kept = filter_reports_by_tag(_reports(con, sin, otra), POLITICA)
    assert [report.market.market_id for report in kept] == ["con", "sin"]
