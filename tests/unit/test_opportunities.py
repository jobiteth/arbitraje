"""Cálculo de oportunidades: spread bruto/neto y descarte de fee desconocida."""

from __future__ import annotations

from datetime import UTC, datetime

from amigocompora.domain.models import (
    Measurement,
    Opportunity,
    PriceComparison,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import BasisPoints, TokenAmount

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _pair() -> TradingPair:
    return TradingPair(
        base=Token("WETH", 18, "ethereum", "0x" + "1" * 40),
        quote=Token("USDC", 6, "ethereum", "0x" + "2" * 40),
    )


def _venue(name: str) -> Venue:
    return Venue(venue_id=name, name=name, kind=VenueKind.DEX, chain="ethereum")


def _quote(pair: TradingPair, venue: str, out: str, fee: int | None) -> Quote:
    return Quote(
        venue=_venue(venue),
        engine_id="dexscreener",
        pair=pair,
        amount_in=TokenAmount.from_decimal(1, 18, "WETH"),
        amount_out=TokenAmount.from_decimal(out, 6, "USDC"),
        fee_bps=BasisPoints(fee) if fee is not None else None,
        fee_basis=Measurement.REPORTED if fee is not None else None,
        price_impact_bps=BasisPoints(5),
        observed_at=NOW,
    )


def test_comparison_ranks_by_amount_out() -> None:
    pair = _pair()
    comp = PriceComparison(
        pair=pair,
        amount_in=TokenAmount.from_decimal(1, 18, "WETH"),
        quotes=(_quote(pair, "a", "100", 30), _quote(pair, "b", "105", 30)),
    )
    assert comp.best.venue.venue_id == "b"
    assert comp.worst.venue.venue_id == "a"
    assert comp.spread_bps.value > 0


def test_opportunity_net_spread_subtracts_fees() -> None:
    pair = _pair()
    best = _quote(pair, "a", "110", 30)
    ref = _quote(pair, "b", "100", 30)
    opp = Opportunity(
        pair=pair,
        amount_in=best.amount_in,
        best=best,
        reference=ref,
        total_fee_bps=BasisPoints(60),
        observed_at=NOW,
    )
    assert opp.gross_spread_bps.value > 0
    assert opp.net_spread_bps.value == opp.gross_spread_bps.value - 60


def test_opportunity_needs_two_venues() -> None:
    import pytest

    from amigocompora.domain.errors import InvalidAmountError

    pair = _pair()
    q = _quote(pair, "a", "100", 30)
    with pytest.raises(InvalidAmountError):
        Opportunity(
            pair=pair,
            amount_in=q.amount_in,
            best=q,
            reference=q,
            total_fee_bps=BasisPoints(60),
            observed_at=NOW,
        )


def test_unknown_fee_marked_in_comparison() -> None:
    pair = _pair()
    comp = PriceComparison(
        pair=pair,
        amount_in=TokenAmount.from_decimal(1, 18, "WETH"),
        quotes=(_quote(pair, "a", "100", None), _quote(pair, "b", "101", 30)),
    )
    # Hay una cotización sin comisión desglosada…
    assert comp.has_unknown_fees
    # …pero el mejor amount_out (b) sí la trae: la bandera es de la comparación,
    # no de la mejor cotización.
    assert comp.best.venue.venue_id == "b"
    assert comp.best.fee_is_known
    unknown = next(q for q in comp.quotes if q.venue.venue_id == "a")
    assert not unknown.fee_is_known
