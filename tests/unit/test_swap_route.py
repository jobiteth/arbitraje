"""La ruta de un swap multi-salto: la estructura y su aritmética.

Estas piezas son de todos los motores que enrutan por más de un pool —V3, V4 y
el V2 directo—, así que se prueban aquí y no dentro del motor de uno:

1. **La forma de la ruta.** Dos tramos o más, encadenados —lo que sale de un
   tramo tiene que entrar en el siguiente— y en una sola red. Una ruta que no
   encadena no llega a ningún sitio, y dejarla construir produciría un calldata
   que nadie puede ejecutar.

2. **La comisión de la ruta** se suma, y con un tramo sin comisión declarada no
   se inventa: se devuelve «no se sabe».

3. **El impacto se compone, no se suma.** Sumarlo diría que dos tramos del 1 %
   cuestan 2 % cuando cuestan 1,99 %; la fórmula que publica una cifra distinta
   de la que se paga es exactamente la clase de número que este proyecto no
   publica.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amigocompora.domain.errors import InvalidAmountError
from amigocompora.domain.models import (
    Measurement,
    Quote,
    RouteHop,
    SwapRoute,
    Token,
    TradingPair,
    Venue,
    VenueKind,
)
from amigocompora.domain.money import BasisPoints, TokenAmount
from amigocompora.engines.uniswap_math import combine_impact_bps

AHORA = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

#: Las direcciones del caso medido en Polygon, en minúsculas.
WPOL = "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"
USDC = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"


def _token(symbol: str, address: str, *, chain_key: str = "polygon") -> Token:
    return Token(symbol=symbol, decimals=18, chain=chain_key, address=address)


def _ruta(tramo_1: RouteHop, tramo_2: RouteHop) -> SwapRoute:
    return SwapRoute(hops=(tramo_1, tramo_2))


def _cadena() -> tuple[RouteHop, RouteHop]:
    wpol = _token("WPOL", WPOL)
    usdc = _token("USDC", USDC)
    pusd = _token("pUSD", PUSD)
    return (
        RouteHop(base=wpol, quote=usdc, fee_bps=BasisPoints(5)),
        RouteHop(base=usdc, quote=pusd, fee_bps=BasisPoints(1)),
    )


# --------------------------------------------------------------------------- #
# 1. La forma de la ruta
# --------------------------------------------------------------------------- #
def test_una_ruta_de_dos_tramos_encadena_sus_tokens() -> None:
    """El camino sale en orden: entrada, hub, salida, y de n tramos n+1 tokens."""
    ruta = _ruta(*_cadena())
    assert [token.symbol for token in ruta.tokens] == ["WPOL", "USDC", "pUSD"]
    assert ruta.total_fee_bps == BasisPoints(6)


def test_un_solo_tramo_no_es_una_ruta() -> None:
    """Un tramo se cotiza como un pool directo; describirlo como ruta confunde
    «por dónde va» con «dónde se ejecuta»."""
    (tramo,) = _cadena()[:1]
    with pytest.raises(InvalidAmountError, match="dos tramos"):
        SwapRoute(hops=(tramo,))


def test_dos_tramos_que_no_encadenan_no_son_una_ruta() -> None:
    """Si el segundo tramo entra por otro token que el que salió del primero,
    el camino no lleva a la salida que cree."""
    primera, _ = _cadena()
    otra = RouteHop(
        base=_token("DAI", "0x" + "d" * 40),
        quote=_token("USDC", USDC),
        fee_bps=BasisPoints(5),
    )
    with pytest.raises(InvalidAmountError, match="no encadenan"):
        SwapRoute(hops=(primera, otra))


def test_un_tramo_a_si_mismo_no_es_un_tramo() -> None:
    """El mismo token no tiene pool contra sí mismo."""
    usdc = _token("USDC", USDC)
    with pytest.raises(InvalidAmountError, match="no es un tramo"):
        RouteHop(base=usdc, quote=usdc)


def test_un_tramo_no_cruza_de_red() -> None:
    """Un pool vive en una red; una ruta que salte de red no es un swap."""
    with pytest.raises(InvalidAmountError, match="cruzar de red"):
        RouteHop(
            base=_token("USDC", USDC, chain_key="polygon"),
            quote=_token("USDC", USDC, chain_key="base"),
        )


def test_sin_comision_declarada_la_ruta_no_inventa_una() -> None:
    """V2 no lleva la comisión en el camino; publicar 0 sería decir «gratis»."""
    primera, segunda = _cadena()
    sin_comision = SwapRoute(
        hops=(
            RouteHop(base=primera.base, quote=primera.quote),
            RouteHop(base=segunda.base, quote=segunda.quote),
        )
    )
    assert sin_comision.total_fee_bps is None


# --------------------------------------------------------------------------- #
# 2. La cotización con ruta
# --------------------------------------------------------------------------- #
def _cotizacion_con_ruta(ruta: SwapRoute | None) -> Quote:
    par = TradingPair(base=_token("WPOL", WPOL), quote=_token("pUSD", PUSD))
    return Quote(
        venue=Venue(
            venue_id="uniswap-v3@5+1",
            name="Uniswap V3 0.05 % + 0.01 %",
            kind=VenueKind.DEX,
            chain="polygon",
        ),
        engine_id="uniswap_v3",
        pair=par,
        amount_in=par.base.amount("1"),
        amount_out=TokenAmount(99594, 6, "pUSD"),
        fee_bps=BasisPoints(6),
        fee_basis=Measurement.DERIVED,
        price_impact_bps=BasisPoints(1),
        impact_basis=Measurement.DERIVED,
        observed_at=AHORA,
        route=ruta,
    )


def test_una_cotizacion_puede_llevar_su_ruta() -> None:
    """`route=None` es «un solo pool»; con ruta, es el camino que se ejecutará."""
    assert _cotizacion_con_ruta(None).route is None
    cotizacion = _cotizacion_con_ruta(_ruta(*_cadena()))
    assert cotizacion.route is not None
    assert [token.symbol for token in cotizacion.route.tokens] == ["WPOL", "USDC", "pUSD"]


def test_una_ruta_de_otra_red_no_se_publica() -> None:
    """Un motor que publique una ruta de otra red construyó un swap que nadie pidió."""
    wpol = _token("WPOL", WPOL, chain_key="base")
    usdc = _token("USDC", USDC, chain_key="base")
    pusd = _token("pUSD", PUSD, chain_key="base")
    ruta = SwapRoute(
        hops=(
            RouteHop(base=wpol, quote=usdc, fee_bps=BasisPoints(5)),
            RouteHop(base=usdc, quote=pusd, fee_bps=BasisPoints(1)),
        )
    )
    with pytest.raises(InvalidAmountError, match="va por"):
        _cotizacion_con_ruta(ruta)


# --------------------------------------------------------------------------- #
# 3. El impacto compuesto
# --------------------------------------------------------------------------- #
def test_el_impacto_de_dos_tramos_compone_y_no_suma() -> None:
    """Dos tramos del 1 % cuestan 1,99 %, no 2 %: el segundo se paga sobre lo
    que sobrevivió al primero."""
    assert combine_impact_bps([BasisPoints(100), BasisPoints(100)]) == BasisPoints(199)


def test_un_solo_tramo_se_compone_consigo_mismo() -> None:
    """La composición de uno es él: la fórmula no puede cambiar el caso simple."""
    assert combine_impact_bps([BasisPoints(37)]) == BasisPoints(37)


def test_un_camino_sin_tramos_no_mueve_ningún_precio() -> None:
    """Cero, no una excepción: no ejecutar nada no tiene impacto."""
    assert combine_impact_bps([]) == BasisPoints(0)
