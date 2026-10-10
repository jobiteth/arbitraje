"""`ScanOpportunities` sobre una comparación ya obtenida no vuelve a cotizar.

La pantalla de precios pide la comparación para enseñarla y, acto seguido, las
oportunidades sobre ella. Volver a pedirla era una segunda ronda completa de la
misma cotización —todos los motores otra vez— y el mayor trozo del tiempo que
se pasaba esperando tras cambiar el importe.

Estas pruebas fijan las dos mitades: con comparación **no** se cotiza —y la
puerta del modo se sigue cruzando, que es lo que autoriza el cálculo—, y sin
ella se cotiza **una sola vez**, que es el camino de `WatchScan`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amigocompora.app.confirmation import ConfirmationGateway, RecordingPrompt
from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.registry import EngineRegistry
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.app.usecases.scan_opportunities import ScanOpportunities
from amigocompora.domain.clock import FrozenClock
from amigocompora.domain.models import (
    Measurement,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.money import BasisPoints, TokenAmount

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _pair() -> TradingPair:
    return TradingPair(
        base=Token("WETH", 18, "ethereum", "0x" + "1" * 40),
        quote=Token("USDC", 6, "ethereum", "0x" + "2" * 40),
    )


def _quote(pair: TradingPair, venue_id: str, out: str) -> Quote:
    return Quote(
        venue=Venue(venue_id=venue_id, name=venue_id, kind=VenueKind.DEX, chain="ethereum"),
        engine_id=venue_id,
        pair=pair,
        amount_in=TokenAmount.from_decimal(1, 18, "WETH"),
        amount_out=TokenAmount.from_decimal(out, 6, "USDC"),
        fee_bps=BasisPoints(5),
        fee_basis=Measurement.REPORTED,
        price_impact_bps=BasisPoints(1),
        impact_basis=Measurement.REPORTED,
        observed_at=NOW,
    )


def _comparison(pair: TradingPair) -> PriceComparison:
    """Dos venues con comisión desglosada y ~37 bps de diferencia.

    Suficiente para que sobreviva una oportunidad al umbral por defecto: es la
    materia prima que `from_comparison` convierte en hallazgos sin tocar la red.
    """
    return PriceComparison(
        pair=pair,
        amount_in=TokenAmount.from_decimal(1, 18, "WETH"),
        quotes=(
            _quote(pair, "a@ethereum", "2700"),
            _quote(pair, "b@ethereum", "2690"),
        ),
    )


def _gateway() -> ConfirmationGateway:
    """Portero de `SIMULACIÓN`, que concede `COMPUTE_ROUTE` sin preguntar.

    Es el modo mínimo en el que el barrido existe —`OBSERVACIÓN` lo bloquea—,
    así que es el que hace pasar la autorización que estas pruebas vigilan.
    """
    return ConfirmationGateway(ModeGuard(OperationMode.SIMULATION), RecordingPrompt())


def _scan(gateway: ConfirmationGateway) -> ScanOpportunities:
    return ScanOpportunities(
        compare_prices=ComparePrices(
            registry=EngineRegistry(ModeGuard(OperationMode.SIMULATION)),
            gateway=gateway,
        ),
        gateway=gateway,
        clock=FrozenClock(),
    )


async def test_con_la_comparacion_en_mano_no_se_vuelve_a_cotizar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dar la comparación evita la segunda ronda: los motores no se tocan.

    El doble sustituye a `ComparePrices.__call__` en la **clase** —el caso de
    uso que se está probando guarda un `ComparePrices` de verdad y `frozen`—,
    así que si alguien volviera a cotizar dentro, la llamada quedaría contada.
    """
    pair = _pair()
    comparison = _comparison(pair)
    llamadas: list[TradingPair] = []

    async def falso(
        self: ComparePrices, pair: TradingPair, amount_in: TokenAmount
    ) -> PriceComparison:
        llamadas.append(pair)
        return comparison

    monkeypatch.setattr(ComparePrices, "__call__", falso, raising=True)
    gateway = _gateway()
    scan = _scan(gateway)

    opportunities = await scan(pair, comparison.amount_in, comparison=comparison)

    assert llamadas == [], "con la comparación en mano no hay nada que volver a pedir"
    assert len(opportunities) == 1
    assert opportunities[0].best.venue.venue_id == "a@ethereum"
    # La puerta del modo se sigue cruzando aunque no haya red de por medio: es
    # la autorización del cálculo, y tiene que quedar en la traza igual.
    assert [record.action.capability for record in gateway.history] == [
        Capability.COMPUTE_ROUTE
    ]


async def test_sin_comparacion_se_cotiza_una_vez(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El camino de `WatchScan` no cambia: cotiza por su cuenta, y una vez."""
    pair = _pair()
    comparison = _comparison(pair)
    llamadas: list[TradingPair] = []

    async def falso(
        self: ComparePrices, pair: TradingPair, amount_in: TokenAmount
    ) -> PriceComparison:
        llamadas.append(pair)
        return comparison

    monkeypatch.setattr(ComparePrices, "__call__", falso, raising=True)
    scan = _scan(_gateway())

    opportunities = await scan(pair, comparison.amount_in)

    assert llamadas == [pair]
    assert len(opportunities) == 1
