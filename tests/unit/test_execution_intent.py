"""Qué importe se mide contra el tope, y qué sigue siendo obligatorio.

La regla que había antes era una sola: **lo que se entrega tiene que ser una pata
del par**. Es lo que hace que el tope se aplique sobre un hecho —lo que de verdad
sale de la cartera— y no sobre una estimación. Con pares que tocan la stablecoin
de referencia esa pata *es* el importe del tope, así que la regla bastaba.

Deja de bastar en cuanto se opera con un par que no la toca: entregar medio ETH
contra un token deja el importe en ETH, y compararlo contra un tope escrito en
dólares sería comparar peras con manzanas —un `max_quote_per_trade` de 1 000
dejaría pasar sin pestañear medio ETH, porque `0,5 < 1 000`—. De ahí
`reference_value`: el valor de lo entregado en la unidad del tope.

Lo que se comprueba aquí es que **la regla vieja sigue en pie**. Añadir un campo
nuevo al lado de una invariante es la forma habitual de perderla: el campo nuevo
parece «el importe de verdad» y el viejo se vuelve un trámite. Aquí no: el
importe entregado sigue teniendo que ser una pata del par aunque haya valoración,
porque si dejara de serlo nadie sabría qué se está midiendo.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError
from amigocompora.domain.models import (
    ExecutionIntent,
    Quote,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import quote_token, wrapped_native

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: El token del ejemplo medido en Base: no tiene piscina contra USDC, así que su
#: par de verdad es contra el envoltorio nativo y lo que se entrega son ETH.
BANKR = Token(
    symbol="bankr",
    decimals=18,
    chain="base",
    address="0x26f79444595cF1C6753bAEd09DbC0C6F73144443",
)


def _quote(*, contra: Token | None = None) -> Quote:
    """El par de siempre —WETH contra la stablecoin— o el que se diga."""
    base = wrapped_native("base")
    quote = contra or quote_token("base")
    assert base is not None
    assert quote is not None
    pair = TradingPair(base=base, quote=quote)
    return Quote(
        venue=Venue(
            venue_id="fake@base",
            name="venue falso",
            kind=VenueKind.DEX,
            chain="base",
        ),
        engine_id="fake",
        pair=pair,
        amount_in=pair.base.amount("1"),
        amount_out=pair.quote.amount("3000"),
        fee_bps=None,
        fee_basis=None,
        price_impact_bps=None,
        impact_basis=None,
        observed_at=NOW,
    )


def _intent(
    quote: Quote,
    *,
    entregado: TokenAmount,
    valorado: TokenAmount | None = None,
) -> ExecutionIntent:
    return ExecutionIntent(
        quote=quote,
        recipient="0x1111111111111111111111111111111111111111",
        notional=entregado,
        engine_id="fake",
        reference_value=valorado,
    )


def _usdc(amount: str) -> TokenAmount:
    base = quote_token("base")
    assert base is not None
    return base.amount(amount)


# --------------------------------------------------------------------------- #
# Lo que se mide
# --------------------------------------------------------------------------- #
def test_sin_valoracion_se_mide_lo_entregado() -> None:
    """El caso de siempre: la pata que coincide con el tope es el importe y ya.

    No hay dos números que puedan discrepar, así que `measured` tiene que
    devolver exactamente lo entregado —no una copia con otro símbolo, ni una
    reconversión—.
    """
    entregado = _usdc("1500")

    intent = _intent(_quote(), entregado=entregado)

    assert intent.measured == entregado
    assert intent.measured is entregado


def test_con_valoracion_se_mide_la_valoracion() -> None:
    """Con un par que no toca el tope, manda el valor —no el importe crudo.

    Y los dos números son distintos a propósito: el importe entregado es `1`
    (ETH), que cabría en cualquier tope pensado en dólares; el valor es 2 500
    USDC, que es lo que de verdad se está gastando.
    """
    base = wrapped_native("base")
    assert base is not None
    entregado = base.amount("1")
    valor = _usdc("2500")

    intent = _intent(_quote(contra=BANKR), entregado=entregado, valorado=valor)

    assert intent.measured == valor
    assert intent.notional == entregado, "lo entregado no se reescribe"


# --------------------------------------------------------------------------- #
# Lo que sigue siendo obligatorio
# --------------------------------------------------------------------------- #
def test_lo_entregado_tiene_que_ser_una_pata_aunque_haya_valoracion() -> None:
    """La invariante de antes no se relaja por añadir un campo al lado.

    Si el importe entregado pudiera ser una tercera moneda —la valoración, por
    ejemplo— el tope se estaría midiendo sobre algo que no sale de ninguna pata
    del par que se va a ejecutar, y nadie sabría qué se está midiendo.
    """
    base = wrapped_native("base")
    assert base is not None

    with pytest.raises(CurrencyMismatchError):
        _intent(
            _quote(contra=BANKR),
            entregado=_usdc("2500"),
            valorado=_usdc("2500"),
        )


def test_una_valoracion_no_positiva_se_rechaza() -> None:
    """Un valor que no es positivo no es un valor: sería un tope que no corta.

    Cero medido contra un tope siempre cabe, así que una valoración a cero
    dejaría pasar la operación entera sin gastar tope. Es un fallo del que valora
    —una cotización que no llegó— y tiene que explotar donde se construye, no
    más tarde y en otro sitio.
    """
    base = wrapped_native("base")
    assert base is not None

    with pytest.raises(InvalidAmountError):
        _intent(
            _quote(contra=BANKR),
            entregado=base.amount("1"),
            valorado=_usdc("0"),
        )
