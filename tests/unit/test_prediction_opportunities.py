"""Tests de las oportunidades de compra en mercados de predicción.

Aritmética pura sobre objetos del dominio: sin red, sin motores, sin reloj real.
Lo que se fija aquí es la condición de arbitraje —una cesta que cuesta menos de
lo que paga— y que un mercado que cobra margen no se confunda con ella.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.analyze_prediction_market import (
    AnalyzePredictionMarkets,
    MarketReport,
)
from amigocompora.app.usecases.find_prediction_opportunities import (
    DEFAULT_MIN_EDGE_BPS,
    FindPredictionOpportunities,
)
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    MarketOutcome,
    PredictionMarket,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import OperationMode
from amigocompora.domain.money import BasisPoints

OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

VENUE = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain="polygon",
)


def _market(*prices: str, question: str = "¿Ocurrirá?") -> PredictionMarket:
    return PredictionMarket(
        market_id="m1",
        venue=VENUE,
        question=question,
        outcomes=tuple(
            MarketOutcome(label=f"Resultado {i}", price=Decimal(price))
            for i, price in enumerate(prices)
        ),
        observed_at=OBSERVED_AT,
    )


def _report(market: PredictionMarket) -> MarketReport:
    return MarketReport(
        market=market,
        overround_bps=market.overround_bps,
        is_coherent=market.is_coherent(),
        favourite=market.favourite,
    )


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
