"""Qué es «el mismo token»: la dirección, no el nombre.

Existe porque el nombre no basta y el caso está medido. En Polygon el USDC
nativo y el USDC puenteado desde Ethereum son dos contratos distintos que
publican los dos `symbol() == "USDC"`, y sólo uno de los dos —el puenteado— es
el colateral que acepta Polymarket. Decidir por símbolo hacía **inexpresable** el
par que los cambia: la aplicación sabía cotizarlo por dirección, pero el dominio
lo rechazaba antes de llegar a intentarlo.

Estas pruebas fijan la regla en los dos sentidos, porque los dos importan:
confundir dos tokens distintos manda dinero al sitio equivocado, y separar dos
veces el mismo token rompe operaciones que son normales.
"""

from __future__ import annotations

import pytest

from amigocompora.domain.errors import CurrencyMismatchError
from amigocompora.domain.models import Token, TradingPair

#: Los dos USDC de Polygon: el nativo y el puenteado, con la dirección tal como
#: la publica el contrato (EIP-55) y en minúsculas, que es como la lee la cadena.
USDC_NATIVO = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_PUENTEADO = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def _nativo() -> Token:
    return Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_NATIVO)


def _puenteado() -> Token:
    return Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_PUENTEADO)


# --------------------------------------------------------------------------- #
# La identidad
# --------------------------------------------------------------------------- #
def test_same_symbol_different_contract_is_not_the_same_token() -> None:
    """El caso que motivó todo esto."""
    assert not _nativo().is_same_asset(_puenteado())
    assert not _puenteado().is_same_asset(_nativo())


def test_same_contract_is_the_same_token_regardless_of_case() -> None:
    """El *checksum* EIP-55 es presentación, no identidad.

    Las fuentes discrepan: la API devuelve la dirección con checksum, la cadena
    la tiene en minúsculas y una configuración escrita a mano puede traer
    cualquiera de las dos. Si eso cambiara la identidad, el mismo token contaría
    como dos según por dónde hubiera entrado.
    """
    a = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_NATIVO)
    b = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_NATIVO.lower())
    assert a.is_same_asset(b)
    assert b.is_same_asset(a)


def test_a_token_is_the_same_as_itself() -> None:
    nativo = _nativo()
    assert nativo.is_same_asset(nativo)


def test_two_natives_of_the_same_chain_are_the_same_token() -> None:
    """El nativo no tiene dirección, y eso no lo hace incomparable.

    Dos tokens nativos de la misma red con el mismo símbolo son el mismo token:
    no hay otro sitio donde puedan estar. Compararlo por `None == None` es lo
    correcto, no un descuido.
    """
    uno = Token(symbol="ETH", decimals=18, chain="base")
    otro = Token(symbol="ETH", decimals=18, chain="base")
    assert uno.is_same_asset(otro)


def test_a_native_is_not_its_wrapped_contract() -> None:
    """`ETH` y `WETH` son tokens distintos aunque se cambien uno por otro.

    Comparten el símbolo sólo por convención —`WETH` contra `ETH` no—, pero el
    caso que sí importa es que un nativo nunca puede ser igual a un contrato:
    son dos cosas distintas en la cadena, una con dirección y otra sin ella.
    """
    nativo = Token(symbol="ETH", decimals=18, chain="base")
    envuelto = Token(symbol="ETH", decimals=18, chain="base", address="0x" + "11" * 20)
    assert not nativo.is_same_asset(envuelto)
    assert not envuelto.is_same_asset(nativo)


def test_the_same_contract_on_another_chain_is_another_token() -> None:
    """Una dirección sólo significa algo dentro de su red."""
    aqui = Token(symbol="USDC", decimals=6, chain="polygon", address=USDC_NATIVO)
    alla = Token(symbol="USDC", decimals=6, chain="base", address=USDC_NATIVO)
    assert not aqui.is_same_asset(alla)


def test_different_symbols_are_different_tokens() -> None:
    usdc = _nativo()
    dai = Token(symbol="DAI", decimals=18, chain="polygon", address=USDC_NATIVO)
    assert not usdc.is_same_asset(dai)


# --------------------------------------------------------------------------- #
# El par
# --------------------------------------------------------------------------- #
def test_a_pair_of_two_homonyms_is_expressible() -> None:
    """Lo que antes era imposible: cambiar un USDC por el otro."""
    par = TradingPair(base=_nativo(), quote=_puenteado())
    assert par.base.address != par.quote.address


def test_a_pair_of_the_same_token_is_still_refused() -> None:
    """La comprobación sigue existiendo, ahora sobre lo que de verdad importa."""
    with pytest.raises(CurrencyMismatchError):
        TradingPair(base=_nativo(), quote=_nativo())


def test_a_pair_of_the_same_token_across_chains_reports_the_chain() -> None:
    """Con la misma dirección en dos redes, el error útil es el de la red.

    Antes salía «un par no puede ser USDC contra sí mismo», que manda a mirar el
    sitio equivocado: el problema no es que los símbolos coincidan, es que son
    dos tokens de redes distintas y no hay pool que los una.
    """
    alla = Token(symbol="USDC", decimals=6, chain="base", address=USDC_NATIVO)
    with pytest.raises(CurrencyMismatchError, match="no hay pool"):
        TradingPair(base=_nativo(), quote=alla)


def test_a_pair_of_homonyms_says_which_is_which() -> None:
    """`USDC/USDC` no sirve ni en la tabla ni en el registro de ejecuciones.

    El registro guarda la etiqueta del par, y es lo único que quedará dentro de
    un año para saber qué se cambió. Dos símbolos iguales ahí son una fila que no
    se puede interpretar.
    """
    par = TradingPair(base=_nativo(), quote=_puenteado())
    assert par.symbol != "USDC/USDC"
    # Se compara sin distinguir mayúsculas a propósito: lo que se muestra es la
    # dirección tal como llegó —con su *checksum* EIP-55, que es la forma en la
    # que una persona puede verificarla a ojo—, y lo que importa aquí es que las
    # dos aparezcan, no en qué caja.
    etiqueta = par.symbol.lower()
    assert "0x3c499c54" in etiqueta
    assert "0x2791bca1" in etiqueta


def test_a_pair_of_different_symbols_keeps_the_plain_label() -> None:
    """La etiqueta larga es para el caso raro; el normal no se ensucia."""
    envuelto = Token(symbol="WETH", decimals=18, chain="polygon", address="0x" + "22" * 20)
    assert TradingPair(base=envuelto, quote=_nativo()).symbol == "WETH/USDC"


def test_a_native_leg_in_a_homonym_pair_is_spelled_out() -> None:
    """Un nativo no tiene dirección con la que desempatar, así que se dice."""
    nativo = Token(symbol="ETH", decimals=18, chain="base")
    contrato = Token(symbol="ETH", decimals=18, chain="base", address="0x" + "33" * 20)
    assert TradingPair(base=nativo, quote=contrato).symbol == "ETH (nativo)/ETH 0x33333333…"
