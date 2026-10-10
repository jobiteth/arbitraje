"""La tendencia y la actividad del mercado: datos para mirar, no para operar.

La tarjeta y la tabla enseñan cuándo se creó el mercado, su volumen —total y de
las últimas 24 h—, la liquidez, el diferencial y cuánto se ha movido su precio.
Ninguno de esos datos decide si un mercado se puede operar, y por eso el modelo
los admite ausentes: una fuente que no los publique deja un hueco que la
interfaz pinta como «—», nunca un cero inventado —un cero afirmaría un dato que
nadie dio—.

Lo que sí se valida es que, cuando vienen, sean creíbles: cifras no negativas y
cambios de precio dentro de [-1, 1], porque son deltas entre dos precios de
[0, 1] y uno fuera de ahí pintaría una tendencia imposible. La etiqueta tiene su
propia regla —identificador, nombre y slug, los tres—: sin el identificador no
se puede pedir el filtro a la fuente, y sin el slug no se puede comprobar de
este lado lo que llegó.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.models import (
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionSort,
    Venue,
    VenueKind,
)

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

VENUE = Venue(
    venue_id="polymarket",
    name="Polymarket",
    kind=VenueKind.PREDICTION_MARKET,
    chain="polygon",
)

POLITICA = MarketTag(tag_id="2", label="Politics", slug="politics")
ELECCIONES = MarketTag(tag_id="82", label="Elections", slug="elections")


def _mercado(
    *,
    created_at: datetime | None = None,
    volume: Decimal | None = None,
    volume_24h: Decimal | None = None,
    liquidity: Decimal | None = None,
    spread: Decimal | None = None,
    price_change_1h: Decimal | None = None,
    price_change_24h: Decimal | None = None,
    price_change_1w: Decimal | None = None,
    tags: tuple[MarketTag, ...] = (),
) -> PredictionMarket:
    return PredictionMarket(
        market_id="m1",
        venue=VENUE,
        question="¿Ocurrirá?",
        outcomes=(
            MarketOutcome(label="Sí", price=Decimal("0.6")),
            MarketOutcome(label="No", price=Decimal("0.4")),
        ),
        observed_at=AHORA,
        created_at=created_at,
        volume=volume,
        volume_24h=volume_24h,
        liquidity=liquidity,
        spread=spread,
        price_change_1h=price_change_1h,
        price_change_24h=price_change_24h,
        price_change_1w=price_change_1w,
        tags=tags,
    )


# --------------------------------------------------------------------------- #
# Los huecos: ausente no es cero
# --------------------------------------------------------------------------- #
def test_the_activity_fields_default_to_absent() -> None:
    """Lo que la fuente no publica no se inventa: `None`, no cero.

    Un cero es una afirmación —«nadie ha negociado»— y `None` es un hueco
    —«no consta»—. La tabla pinta uno como 0 y el otro como «—», así que
    confundirlos enseñaría actividad que no se ha medido.
    """
    mercado = _mercado()
    assert mercado.created_at is None
    assert mercado.volume is None
    assert mercado.volume_24h is None
    assert mercado.liquidity is None
    assert mercado.spread is None
    assert mercado.price_change_1h is None
    assert mercado.price_change_24h is None
    assert mercado.price_change_1w is None
    assert mercado.tags == ()


def test_a_naive_creation_date_is_refused() -> None:
    """Ordenar por «más nuevas» es comparar fechas, y sin zona no es una resta."""
    # El `noqa` es el punto de la prueba: aquí el dato sin zona es el que se
    # construye a propósito, no un descuido que la regla deba cazar.
    sin_zona = datetime(2026, 5, 1, 9, 0)  # noqa: DTZ001
    with pytest.raises(InvalidAmountError, match="zona horaria"):
        _mercado(created_at=sin_zona)


# --------------------------------------------------------------------------- #
# Las cifras: creíbles o fuera
# --------------------------------------------------------------------------- #
#: Construye el mercado cifrando cada campo de actividad, para probar la misma
#: regla en los cuatro sin repetir el cuerpo de la prueba.
FABRICAS: tuple[tuple[str, Callable[[Decimal], PredictionMarket]], ...] = (
    ("volume", lambda valor: _mercado(volume=valor)),
    ("volume_24h", lambda valor: _mercado(volume_24h=valor)),
    ("liquidity", lambda valor: _mercado(liquidity=valor)),
    ("spread", lambda valor: _mercado(spread=valor)),
)


@pytest.mark.parametrize(
    "fabrica", [fabrica for _, fabrica in FABRICAS], ids=[nombre for nombre, _ in FABRICAS]
)
@pytest.mark.parametrize("valor", [Decimal("-1"), Decimal("Infinity")])
def test_a_nonsense_activity_figure_is_refused(
    fabrica: Callable[[Decimal], PredictionMarket], valor: Decimal
) -> None:
    """Ni negativa ni infinita: una cifra así no es una cantidad de nada."""
    with pytest.raises(InvalidAmountError, match="negativo"):
        fabrica(valor)


def test_a_price_change_outside_the_scale_is_refused() -> None:
    """Es un delta entre dos precios de [0, 1]: fuera de [-1, 1] no cabe."""
    for fuera in ("1.5", "-1.5"):
        with pytest.raises(InvalidAmountError, match="delta"):
            _mercado(price_change_24h=Decimal(fuera))


def test_the_edges_of_the_scale_are_valid_deltas() -> None:
    """De 1 a 0 —o al revés— es el cambio más grande posible, y existe."""
    mercado = _mercado(price_change_1h=Decimal("-1"), price_change_1w=Decimal("1"))
    assert mercado.price_change_1h == Decimal("-1")
    assert mercado.price_change_1w == Decimal("1")


# --------------------------------------------------------------------------- #
# Las etiquetas
# --------------------------------------------------------------------------- #
def test_a_tag_with_a_blank_part_is_refused() -> None:
    for campos in (
        {"tag_id": "", "label": "Politics", "slug": "politics"},
        {"tag_id": "2", "label": "  ", "slug": "politics"},
        {"tag_id": "2", "label": "Politics", "slug": ""},
    ):
        with pytest.raises(InvalidAmountError, match="etiqueta"):
            MarketTag(**campos)


def test_repeated_tags_are_refused() -> None:
    """Dos etiquetas con el mismo slug son la misma: repetirla sería contarla dos veces."""
    with pytest.raises(InvalidAmountError, match="repetidas"):
        _mercado(tags=(POLITICA, MarketTag(tag_id="99", label="Politics!", slug="politics")))


def test_tags_arrive_in_their_order() -> None:
    mercado = _mercado(tags=(POLITICA, ELECCIONES))
    assert [tag.slug for tag in mercado.tags] == ["politics", "elections"]


# --------------------------------------------------------------------------- #
# El criterio de orden
# --------------------------------------------------------------------------- #
def test_each_sort_is_its_own_name_on_the_wire() -> None:
    """El valor viaja en la URL y se guarda en los combos: cambiarlo rompe los dos."""
    assert [sort.value for sort in PredictionSort] == [
        "newest",
        "trending",
        "volume",
        "coherence",
    ]
