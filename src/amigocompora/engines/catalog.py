"""Catálogo de tokens y pares sugeridos por red.

**No hay precios aquí.** Un precio escrito en el código es un precio falso en
cuanto se guarda el fichero; los precios se leen de la fuente, siempre.

Lo que sí hay son direcciones y decimales, y **los decimales son el dato crítico
de este fichero**. Un decimal equivocado no produce un error: produce un precio
mil veces mayor o menor y una comparación que parece perfectamente correcta.
Por eso cada entrada de abajo está medida en la propia fuente —el campo
`decimals` que publica GeckoTerminal para ese contrato, o el `decimal_place` de
`detail_platforms` en CoinGecko— y ninguna está escrita de memoria.

Dos cosas que la medición enseñó y que conviene no olvidar al añadir entradas:

- **USDT en BNB Chain tiene 18 decimales**, no 6. Y USDC en BNB Chain, también
  18. Dar por hecho que «las stablecoins llevan 6» habría multiplicado por
  10¹² todas las cifras de esa red.
- La misma dirección existe en redes distintas. El USDC de Ethereum devuelve
  pools de PulseChain si se consulta sin filtrar por red, porque PulseChain es
  un fork de Ethereum y hereda sus contratos. De ahí que todo token lleve su
  red y que los motores filtren por ella.

El catálogo no pretende ser exhaustivo: es la vía rápida para los tokens que el
usuario va a pedir el 99 % de las veces, y el punto de partida de los pares
sugeridos. Cualquier otro token se resuelve preguntando a la fuente por su
dirección, que devuelve símbolo y decimales medidos.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from amigocompora.domain.chains import CHAINS, chain
from amigocompora.domain.models import Token, TradingPair


def _token(symbol: str, decimals: int, chain_key: str, address: str) -> Token:
    """Construye un token validando la dirección contra el formato de su red.

    La validación es del formato, no de la existencia: que una dirección sea
    EVM bien formada no dice que el contrato esté ahí. Lo que evita es el error
    tonto de pegar una dirección de Solana en una red EVM, que sin esta
    comprobación acabaría en una petición silenciosamente vacía.
    """
    return Token(
        symbol=symbol,
        decimals=decimals,
        chain=chain_key,
        address=chain(chain_key).require_address(address),
    )


# --------------------------------------------------------------------------- #
# Tokens nativos envueltos, medidos en los pools principales de cada red
# --------------------------------------------------------------------------- #
def wrapped_native(chain_key: str) -> Token | None:
    """Token nativo envuelto de una red, o `None` si no tiene.

    Es el token con el que se cotiza de verdad: los AMM de producto constante
    no operan con el nativo, sino con su envoltorio ERC-20. Arc no tiene
    ninguno porque cobra el gas en USDC.
    """
    spec = chain(chain_key)
    if spec.wrapped_native is None:
        return None
    symbol = "WSOL" if chain_key == "solana" else f"W{spec.native_symbol}"
    return _token(symbol, spec.native_decimals, chain_key, spec.wrapped_native)


# --------------------------------------------------------------------------- #
# Stablecoin de referencia por red
# --------------------------------------------------------------------------- #
#: Con qué se cotiza en cada red, y sus decimales **medidos**.
#:
#: Normalmente USDC, porque es la que existe de forma nativa en más redes. Las
#: excepciones están medidas, no supuestas: en BNB Chain lleva 18 decimales, y
#: en Robinhood Chain la stablecoin con liquidez real es USDG (Global Dollar),
#: que aparece en la mitad de sus pools principales mientras USDC no aparece.
_QUOTE_SPECS: Final[Mapping[str, tuple[str, int, str]]] = {
    "ethereum": ("USDC", 6, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    "base": ("USDC", 6, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
    "bsc": ("USDT", 18, "0x55d398326f99059ff775485246999027b3197955"),
    "arbitrum": ("USDC", 6, "0xaf88d065e77c8cc2239327c5edb3a432268e5831"),
    "polygon": ("USDC", 6, "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"),
    "unichain": ("USDC", 6, "0x078d782b760474a361dda0af3839290b0ef57ad6"),
    "optimism": ("USDC", 6, "0x0b2c639c533813f4aa9d7837caf62653d097ff85"),
    "avalanche": ("USDC", 6, "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"),
    "arc": ("USDC", 6, "0x3600000000000000000000000000000000000000"),
    "robinhood": ("USDG", 6, "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
    "solana": ("USDC", 6, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
}


def quote_token(chain_key: str) -> Token | None:
    """Stablecoin con la que se cotiza en esa red, o `None` si no hay elegida."""
    spec = _QUOTE_SPECS.get(chain_key)
    if spec is None:
        return None
    symbol, decimals, address = spec
    return _token(symbol, decimals, chain_key, address)


def native_token(chain_key: str) -> Token:
    """La moneda nativa de una red: ETH, POL, BNB, SOL. `address=None` la marca.

    Existe porque el motor de cartera la necesita para **todas** las redes, y sin
    este helper cada sitio la reconstruía a mano desde `ChainSpec` con la
    posibilidad de equivocar los decimales —que no da error, da un saldo mil
    veces mayor o menor—. Aquí los decimales salen del registro, que es donde
    están medidos.
    """
    spec = chain(chain_key)
    return Token(
        symbol=spec.native_symbol,
        decimals=spec.native_decimals,
        chain=chain_key,
        address=None,
    )


# --------------------------------------------------------------------------- #
# Tokens adicionales con liquidez medida
# --------------------------------------------------------------------------- #
#: Otros tokens por red, con sus decimales medidos. Todos aparecen en los pools
#: de más volumen de su red, así que todos tienen liquidez real que cotizar.
_EXTRA_SPECS: Final[Mapping[str, Sequence[tuple[str, int, str]]]] = {
    "ethereum": (
        ("WBTC", 8, "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"),
        ("USDT", 6, "0xdac17f958d2ee523a2206206994597c13d831ec7"),
        ("DAI", 18, "0x6b175474e89094c44da98b954eedeac495271d0f"),
    ),
    "base": (
        ("cbBTC", 8, "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"),
        ("USDT", 6, "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2"),
    ),
    "arbitrum": (
        ("WBTC", 8, "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"),
        ("ARB", 18, "0x912ce59144191c1204e64559fe8253a0e49e6548"),
    ),
    # En Polygon el USDC existe dos veces y no son el mismo token: el nativo,
    # que es la stablecoin de referencia de la red, y el puenteado desde
    # Ethereum («USDC.e»). Los dos contratos publican `symbol() == "USDC"` —eso
    # está medido, no supuesto—, así que se distinguen por dirección, que es lo
    # que hace `Token.is_same_asset`. El puenteado entra en el catálogo porque
    # tiene la liquidez y porque es el **colateral que acepta Polymarket**:
    # sin él, cambiar USDC nativo por colateral exigiría pegar la dirección a
    # mano, y su par contra el nativo es una de las piscinas de más volumen de
    # la red. Va después del nativo para que quien busque «USDC» a secas —una
    # configuración, por ejemplo— encuentre el nativo, que es el de referencia.
    "polygon": (
        ("USDC", 6, "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"),
        ("pUSD", 6, "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"),
    ),
    "unichain": (("WBTC", 8, "0x0555e30da8f98308edb960aa94c0db47230d2b9c"),),
    "optimism": (("OP", 18, "0x4200000000000000000000000000000000000042"),),
    "avalanche": (
        ("BTC.b", 8, "0x152b9d0fdc40c096757f570a51e494bd4b943e50"),
        ("WETH.e", 18, "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab"),
    ),
    "arc": (("cirBTC", 8, "0x171a4217b86a807a64eb94757db6849fb4bdbaa0"),),
    # En Solana el catálogo se queda corto a propósito. Medido el 2026-10-07
    # contra GeckoTerminal: la primera página de piscinas por volumen de la red
    # es casi toda memecoin de pump.fun —«Sand Witch Kitten», «Baby Baton»—, y
    # meterlas aquí las convertiría en parte de la lista de tradeables de la
    # aplicación. Eso no es liquidez que cotizar: es un token que puede irse a
    # cero entre la cotización y la firma. Se añaden sólo los tres medidos que
    # llevan años en la red, y lo demás se resuelve al leer: en Solana una
    # dirección **sí** enumera sus tokens, así que la cartera los descubre sin
    # necesidad de tenerlos en una lista escrita de antemano.
    "solana": (
        ("USDT", 6, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),
        ("ORCA", 6, "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"),
        ("RAY", 6, "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"),
    ),
}


def tokens_for(chain_key: str) -> tuple[Token, ...]:
    """Todos los tokens del catálogo en una red, el envoltorio nativo primero."""
    found: list[Token] = []
    native = wrapped_native(chain_key)
    if native is not None:
        found.append(native)
    quote = quote_token(chain_key)
    if quote is not None:
        found.append(quote)
    found.extend(
        _token(symbol, decimals, chain_key, address)
        for symbol, decimals, address in _EXTRA_SPECS.get(chain_key, ())
    )
    return tuple(found)


def token_by_symbol(symbol: str, chain_key: str) -> Token | None:
    """Busca un token del catálogo por símbolo, sin distinguir mayúsculas."""
    wanted = symbol.strip().lower()
    for token in tokens_for(chain_key):
        if token.symbol.lower() == wanted:
            return token
    return None


def token_by_address(address: str, chain_key: str) -> Token | None:
    """Busca un token del catálogo por dirección, con la regla de su red.

    La comparación la hace la red, no esta función: en EVM se normaliza a
    minúsculas porque las fuentes discrepan en el *checksum* EIP-55, y en
    Solana se compara tal cual porque base58 distingue mayúsculas.
    """
    spec = CHAINS.get(chain_key)
    if spec is None:
        return None
    wanted = spec.normalize_address(address)
    for token in tokens_for(chain_key):
        if token.address is not None and spec.normalize_address(token.address) == wanted:
            return token
    return None


# --------------------------------------------------------------------------- #
# Pares sugeridos
# --------------------------------------------------------------------------- #
def suggested_pairs(chain_key: str) -> tuple[TradingPair, ...]:
    """Pares con los que abrir la vista de precios de una red.

    Se construyen contra la stablecoin de referencia porque es el denominador
    que hace comparables los precios de todos los venues. Si una red no tiene
    stablecoin elegida, no se sugiere nada: antes una lista vacía que un par
    sin liquidez que haga parecer que la red no funciona.
    """
    quote = quote_token(chain_key)
    if quote is None:
        return ()
    return tuple(
        TradingPair(base=token, quote=quote)
        for token in tokens_for(chain_key)
        if not token.is_same_asset(quote)
    )


def chains_with_pairs() -> tuple[str, ...]:
    """Redes para las que el catálogo sabe sugerir al menos un par."""
    return tuple(key for key in CHAINS if suggested_pairs(key))
