"""Aritmética monetaria: la regla «ningún float» y los redondeos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError
from amigocompora.domain.money import BasisPoints, Price, TokenAmount


def test_decimal_scaling_is_exact() -> None:
    amount = TokenAmount.from_decimal(Decimal("1.5"), 18, "ETH")
    assert amount.raw == 1_500_000_000_000_000_000
    assert amount.as_decimal() == Decimal("1.5")


def test_float_is_rejected() -> None:
    with pytest.raises(InvalidAmountError):
        TokenAmount(raw=1.0, decimals=18, symbol="ETH")  # type: ignore[arg-type]


def test_implicit_rounding_is_an_error() -> None:
    with pytest.raises(InvalidAmountError):
        TokenAmount.from_decimal(Decimal("0.0000000000000000001"), 18, "ETH")


def test_combining_different_tokens_fails() -> None:
    a = TokenAmount.from_decimal(1, 6, "USDC")
    b = TokenAmount.from_decimal(1, 18, "DAI")
    with pytest.raises(CurrencyMismatchError):
        _ = a + b


def test_negative_amount_arithmetic() -> None:
    zero = TokenAmount.zero(18, "ETH")
    one = TokenAmount.from_decimal(1, 18, "ETH")
    assert (one - one) == zero
    assert one.is_positive
    assert not zero.is_positive


def test_basis_points_ratio_and_percent() -> None:
    bps = BasisPoints(50)
    assert bps.as_percent() == Decimal("0.5")
    assert BasisPoints.from_percent(Decimal("0.5")) == bps
    assert BasisPoints.from_ratio(Decimal("0.005")) == bps


def test_price_difference_in_bps() -> None:
    low = Price(Decimal("100"), "ETH", "USDC")
    high = Price(Decimal("101"), "ETH", "USDC")
    assert high.difference_from(low) == BasisPoints(100)


def test_price_of_wrong_pair_fails() -> None:
    a = Price(Decimal("1"), "ETH", "USDC")
    b = Price(Decimal("1"), "BTC", "USDC")
    with pytest.raises(CurrencyMismatchError):
        a.difference_from(b)
