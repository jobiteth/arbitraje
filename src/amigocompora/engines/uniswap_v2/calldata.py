"""La ABI de Uniswap V2 que este motor usa, con los selectores **medidos**.

### Los selectores se calculan y luego se contrastan

Se derivan con keccak de la firma, como en `infra.evm.broadcast`, y además se
comprueban contra el valor que **se midió en el bytecode desplegado** —cada uno
buscado literalmente dentro del dispatcher, que es donde Solidity lo lleva—. Las
dos cosas juntas son más fuertes que cualquiera de las dos sola: calcular evita
el selector recordado a mano, y contrastar evita el error de teclear mal la firma
—que produciría un selector perfectamente válido y perfectamente equivocado—. Si
alguno no cuadra, el módulo **no importa**. Fallar al importar es el sitio barato
para fallar esto.

Medido el 2026-10-09 en los seis routers de `addresses.DEPLOYMENTS`, y además
**en funcionamiento**: `getPair`, `getReserves`, `token0` y `getAmountsOut`
devolvieron datos coherentes con sus pools, y los tres `swap*` se llamaron con
`eth_call` y revirtieron con una razón del cuerpo de la función —fondos, salida
insuficiente—, o sea que el dispatcher lleva cada selector a la función que se
cree y no a otra.

### El `path[]` sí es una lista de direcciones

Al contrario que el `bytes path` empaquetado del multi-salto de V3 —token de 20
bytes, comisión de 3, sin longitud por medio—, aquí la ABI codifica un
`address[]` de verdad: palabras de 32 bytes, la longitud delante y cada token en
la suya. Se valida al codificar con `word_address`, que exige los 40 dígitos: una
dirección corta no corre los bytes siguientes —no puede, cada una ocupa su
palabra— pero construiría el camino contra otro contrato sin que nada chirriara.

### La comisión no viaja en el calldata

El router de V2 no recibe la comisión: la lleva dentro, y de eso se encarga el
par. La comisión medida (`addresses.Deployment.fee_bps`) se usa para publicarla
en la cotización y para medir el impacto, pero el payload sólo lleva tokens e
importes — y por eso una ruta sin comisión declarada se puede ejecutar igual.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from amigocompora.domain.errors import EngineConfigError, SourceResponseError
from amigocompora.infra.evm.broadcast import selector, word_address, word_uint

GET_PAIR: Final = "getPair(address,address)"
GET_RESERVES: Final = "getReserves()"
TOKEN0: Final = "token0()"
GET_AMOUNTS_OUT: Final = "getAmountsOut(uint256,address[])"
SWAP_EXACT_TOKENS_FOR_TOKENS: Final = (
    "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
)
SWAP_EXACT_ETH_FOR_TOKENS: Final = (
    "swapExactETHForTokens(uint256,address[],address,uint256)"
)
SWAP_EXACT_TOKENS_FOR_ETH: Final = (
    "swapExactTokensForETH(uint256,uint256,address[],address,uint256)"
)

SELECTOR_GET_PAIR: Final = selector(GET_PAIR)
SELECTOR_GET_RESERVES: Final = selector(GET_RESERVES)
SELECTOR_TOKEN0: Final = selector(TOKEN0)
SELECTOR_GET_AMOUNTS_OUT: Final = selector(GET_AMOUNTS_OUT)
SELECTOR_SWAP_EXACT_TOKENS_FOR_TOKENS: Final = selector(SWAP_EXACT_TOKENS_FOR_TOKENS)
SELECTOR_SWAP_EXACT_ETH_FOR_TOKENS: Final = selector(SWAP_EXACT_ETH_FOR_TOKENS)
SELECTOR_SWAP_EXACT_TOKENS_FOR_ETH: Final = selector(SWAP_EXACT_TOKENS_FOR_ETH)

#: Lo que se **midió**: cada selector buscado literalmente dentro del bytecode
#: desplegado y después ejercitado contra el contrato, como cuenta el docstring
#: del módulo. Medido el 2026-10-09.
_MEASURED: Final[Mapping[str, str]] = {
    GET_PAIR: "0xe6a43905",
    GET_RESERVES: "0x0902f1ac",
    TOKEN0: "0x0dfe1681",
    GET_AMOUNTS_OUT: "0xd06ca61f",
    SWAP_EXACT_TOKENS_FOR_TOKENS: "0x38ed1739",
    SWAP_EXACT_ETH_FOR_TOKENS: "0x7ff36ab5",
    SWAP_EXACT_TOKENS_FOR_ETH: "0x18cbafe5",
}


def _require_measured_selectors() -> None:
    """Comprueba al importar que ninguna firma se ha desviado de lo medido."""
    wrong = [
        f"{signature} → {selector(signature)} (se midió {measured})"
        for signature, measured in _MEASURED.items()
        if selector(signature) != measured
    ]
    if wrong:
        raise EngineConfigError(
            "la ABI de Uniswap V2 no coincide con la que se midió en la cadena: "
            + "; ".join(wrong)
            + ". No se importa el motor: un selector desviado llama a otra "
            "función del mismo contrato y no se nota hasta que hay fondos dentro."
        )


_require_measured_selectors()


# --------------------------------------------------------------------------- #
# Codificación
# --------------------------------------------------------------------------- #
def get_pair(token_a: str, token_b: str) -> str:
    """`getPair(tokenA, tokenB)`, para un `eth_call` a la fábrica.

    El orden de los tokens no importa: la fábrica ordena por dirección antes de
    calcular la del pool, así que `(A, B)` y `(B, A)` dan el mismo contrato. La
    dirección cero como respuesta es «no hay pool», que es una respuesta normal.
    """
    return SELECTOR_GET_PAIR + word_address(token_a) + word_address(token_b)


def get_reserves() -> str:
    """`getReserves()` del par, para un `eth_call`.

    Devuelve las dos reservas **en el orden de `token0`/`token1`**, que no es el
    de la operación: cuál es cuál lo dice `token0()`, y de eso depende que el
    marginal del impacto se calcule en el sentido correcto.
    """
    return SELECTOR_GET_RESERVES


def token0() -> str:
    """`token0()` del par: cuál de los dos tokens abre el par.

    Se lee en vez de deducirse. La fábrica ordena los tokens por dirección al
    crear el pool, así que comparar direcciones daría lo mismo hoy; leerlo lo
    sigue dando si esa regla cambiara, y una comparación invertida produciría un
    impacto de precio con el signo cambiado sin que nada chirriara.
    """
    return SELECTOR_TOKEN0


def get_amounts_out(amount_in_raw: int, path: Sequence[str]) -> str:
    """`getAmountsOut(amountIn, path)`, para un `eth_call` al router.

    V2 no tiene un contrato cotizador aparte —el QuoterV2 de V3 no existe
    aquí—: la función que cotiza vive en el propio router y es la **misma
    aritmética** que ejecutará el swap, sobre el estado del bloque actual. Por
    eso el número que se publica es el que la cadena da y no una fórmula de este
    lado; la fórmula sólo se usa en las pruebas, para comprobar que el contrato
    desplegado es el que la tabla dice.
    """
    return (
        SELECTOR_GET_AMOUNTS_OUT
        + word_uint(amount_in_raw)
        + word_uint(64)
        + _path_words(path)
    )


def swap_exact_tokens_for_tokens(
    *,
    amount_in_raw: int,
    amount_out_min_raw: int,
    path: Sequence[str],
    recipient: str,
    deadline: int,
) -> str:
    """`swapExactTokensForTokens(amountIn, amountOutMin, path, to, deadline)`.

    El router cobra el token él mismo con `transferFrom` —medido: la llamada sin
    fondos revierte en la transferencia—, así que basta un `approve` del ERC-20
    contra él. El camino es la lista de tokens a cruzar, en orden.
    """
    return (
        SELECTOR_SWAP_EXACT_TOKENS_FOR_TOKENS
        + word_uint(amount_in_raw)
        + word_uint(amount_out_min_raw)
        + word_uint(0xA0)
        + word_address(recipient)
        + word_uint(deadline)
        + _path_words(path)
    )


def swap_exact_eth_for_tokens(
    *,
    amount_out_min_raw: int,
    path: Sequence[str],
    recipient: str,
    deadline: int,
) -> str:
    """`swapExactETHForTokens(amountOutMin, path, to, deadline)`.

    El importe de entrada **no va en la llamada**: es el `value` que la
    transacción lleva, y el router lo envuelve él mismo. El primer token del
    camino tiene que ser el envuelto nativo, o el router rechaza el camino
    (`INVALID_PATH`, medido por el contrato): el nativo se compra con el nativo
    que viaja, no con una dirección que no existe.
    """
    return (
        SELECTOR_SWAP_EXACT_ETH_FOR_TOKENS
        + word_uint(amount_out_min_raw)
        + word_uint(0x80)
        + word_address(recipient)
        + word_uint(deadline)
        + _path_words(path)
    )


def swap_exact_tokens_for_eth(
    *,
    amount_in_raw: int,
    amount_out_min_raw: int,
    path: Sequence[str],
    recipient: str,
    deadline: int,
) -> str:
    """`swapExactTokensForETH(amountIn, amountOutMin, path, to, deadline)`.

    El último token del camino tiene que ser el envuelto nativo, y el router lo
    retira **él mismo** antes de mandar el nativo al destinatario: aquí no hay
    `unwrapWETH9` ni `multicall` que valga, porque el swap termina en el nativo
    dentro de la misma función.
    """
    return (
        SELECTOR_SWAP_EXACT_TOKENS_FOR_ETH
        + word_uint(amount_in_raw)
        + word_uint(amount_out_min_raw)
        + word_uint(0xA0)
        + word_address(recipient)
        + word_uint(deadline)
        + _path_words(path)
    )


def _path_words(path: Sequence[str]) -> str:
    """El `address[]` del camino: longitud y una dirección por palabra.

    Se valida la forma y no se supone: un camino de un solo token no une nada
    —no hay swap que hacer— y `word_address` exige los 40 dígitos de cada
    dirección, porque una dirección mal formada construiría el camino contra
    otro contrato sin que nada lo delatara al codificar.
    """
    if len(path) < 2:
        raise SourceResponseError(
            f"un camino de {len(path)} token(s) no une nada: un swap necesita al "
            f"menos el token de entrada y el de salida"
        )
    return word_uint(len(path)) + "".join(word_address(token) for token in path)


# --------------------------------------------------------------------------- #
# Decodificación
# --------------------------------------------------------------------------- #
def decode_address(raw: str, index: int = 0) -> str | None:
    """La dirección de la palabra `index`, o `None` si la respuesta no llega.

    `None` y no una excepción porque el caso normal de `getPair` es que **no haya
    pool** en ese par, y la fábrica contesta con la dirección cero. Distinguir
    «no hay pool» de «la respuesta está rota» es trabajo de quien llama.
    """
    word = _word(raw, index)
    return None if word is None else "0x" + word[24:]


def decode_uint(raw: str, index: int = 0) -> int | None:
    """El entero de la palabra `index`, o `None` si la respuesta no llega."""
    word = _word(raw, index)
    return None if word is None else int(word, 16)


def decode_reserves(raw: str) -> tuple[int, int] | None:
    """Las dos reservas del par, o `None` si la respuesta está truncada.

    Van juntas y en el orden del contrato —`reserve0`, `reserve1`—; cuál
    corresponde al token que se vende lo decide `token0()`, y por eso el motor
    lee las dos cosas antes de calcular nada.
    """
    reserve0 = decode_uint(raw, 0)
    reserve1 = decode_uint(raw, 1)
    if reserve0 is None or reserve1 is None:
        return None
    return reserve0, reserve1


def decode_amounts_out(raw: str, *, hops: int) -> tuple[int, ...] | None:
    """Los importes que devolvió `getAmountsOut`, uno por token del camino.

    La forma se comprueba y no se supone, porque leerla mal publicaría un número
    de otro sitio: la respuesta tiene que ser **un array dinámico** —desplazamiento
    de una palabra, longitud, y los importes detrás— con exactamente `hops + 1`
    valores, uno por token del camino. Una longitud distinta significa que esa
    respuesta no es la de esta función, y se dice en vez de recortarla.

    Una respuesta **truncada** —no llega ni la longitud— se lee como «no hay
    dato», que es lo que es: un nodo que contesta `0x` a un contrato que no
    conoce. El importe final del camino es el último valor.
    """
    offset = decode_uint(raw, 0)
    if offset is None:
        return None
    if offset != 32:
        raise SourceResponseError(
            f"la respuesta de getAmountsOut empieza por {offset} y un array "
            f"dinámico empieza por 32: esa respuesta no es la de esta función"
        )
    count = decode_uint(raw, 1)
    if count is None:
        return None
    if count != hops + 1:
        raise SourceResponseError(
            f"getAmountsOut devolvió {count} importes para un camino de {hops + 1} "
            f"tokens: esa respuesta no es la de esta función"
        )
    amounts: list[int] = []
    for index in range(count):
        value = decode_uint(raw, 2 + index)
        if value is None:
            return None
        amounts.append(value)
    return tuple(amounts)


def _word(raw: str, index: int) -> str | None:
    """La palabra `index` del retorno, o `None` si no está.

    Una respuesta truncada —un nodo que devuelve `0x` para un contrato que no
    existe— tiene que leerse como «no hay dato» y no como un cero: un cero es un
    valor legítimo y confundirlos haría cotizar un pool vacío.

    Que `raw` sea una cadena ya lo garantiza `ChainReader.eth_call`, que lanza si
    el nodo contesta con otra cosa; repetir la comprobación aquí sería código que
    no se puede alcanzar.
    """
    body = raw[2:] if raw[:2].lower() == "0x" else raw
    start = index * 64
    if len(body) < start + 64:
        return None
    return body[start : start + 64]
