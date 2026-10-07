"""Cuánto queda para que cierre un mercado, que es lo que enseña la cuenta atrás.

Se prueba aparte del motor porque la regla de frontera es del dominio y la usan
los dos: la pestaña, para pintar «en 6 h» o «cerrado», y el motor, para decidir
si un mercado entra en la ventana. Dos respuestas distintas a «¿queda tiempo?»
serían un mercado pintado como abierto que el filtro deja fuera.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.models import (
    MarketOutcome,
    PredictionMarket,
    Venue,
    VenueKind,
)

AHORA = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

VENUE = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain="polygon",
)


def _mercado(closes_at: datetime | None) -> PredictionMarket:
    return PredictionMarket(
        market_id="m1",
        venue=VENUE,
        question="¿Ocurrirá?",
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.6")),
            MarketOutcome(label="No", price=Decimal("0.4")),
        ),
        observed_at=AHORA,
        closes_at=closes_at,
    )


def test_it_counts_the_time_left() -> None:
    assert _mercado(AHORA + timedelta(hours=6)).time_left(AHORA) == timedelta(hours=6)


def test_a_market_without_a_close_date_has_no_time_left() -> None:
    assert _mercado(None).time_left(AHORA) is None


def test_the_closing_instant_itself_counts_as_closed() -> None:
    """Cero no es «queda cero»: es que ya no queda.

    Devolver un `timedelta` de cero invitaría a operar un mercado que en ese
    mismo instante ya no admite órdenes, y la cuenta atrás enseñaría «en 0 s» en
    vez de «cerrado».
    """
    assert _mercado(AHORA).time_left(AHORA) is None


def test_a_market_that_already_closed_has_no_time_left() -> None:
    assert _mercado(AHORA - timedelta(seconds=1)).time_left(AHORA) is None


def test_a_naive_now_is_refused() -> None:
    """Comparar un instante con zona contra uno sin ella no es una resta.

    Python lo rechaza, pero con un `TypeError` que habla de tipos y no de
    zonas horarias, y esto se leería como un fallo del mercado en vez de lo que
    es: un reloj mal construido.
    """
    # El `noqa` es el punto de la prueba: aquí el reloj sin zona es lo que se está
    # construyendo a propósito, no un descuido que la regla deba cazar.
    sin_zona = datetime(2026, 6, 1, 12, 0)  # noqa: DTZ001
    with pytest.raises(InvalidAmountError, match="zona horaria"):
        _mercado(AHORA + timedelta(hours=1)).time_left(sin_zona)
