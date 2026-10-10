"""La ABI de Uniswap V3 que este motor usa, con los selectores **comprobados**.

### Por qué las dos firmas de `exactInputSingle` están aquí y no una

Uniswap V3 tiene dos routers y **no comparten el ABI**:

- El SwapRouter V1 recibe ocho campos, con `deadline` entre `recipient` y
  `amountIn`.
- El SwapRouter02 recibe siete: le quitaron el `deadline`.

No son la misma función con un campo de más. Añadir un `uint256` en medio de una
struct cambia la firma, y por tanto el selector: `0x414bf389` frente a
`0x04e45aaf`. Llamar con el selector equivocado no da un error de compilación ni
un mensaje claro — el contrato ejecuta la función que corresponde a ese selector,
que es otra, o revierte. Es exactamente la clase de fallo que no se ve hasta que
hay dinero dentro, y por eso el motor **elige el codificador por red** según lo
que dice `addresses.DEPLOYMENTS`, que a su vez se midió leyendo el dispatcher.

### Los selectores se calculan y luego se contrastan

Se derivan con keccak de la firma, como en `infra.evm.broadcast`, y además se
comprueban contra el valor que **se midió en el bytecode desplegado**. Las dos
cosas juntas son más fuertes que cualquiera de las dos sola: calcular evita el
selector recordado a mano, y contrastar evita el error de teclear mal la firma
—que produciría un selector perfectamente válido y perfectamente equivocado—.
Si alguno no cuadra, el módulo **no importa**. Fallar al importar es el sitio
barato para fallar esto.

### Los decodificadores viven aquí, y no en `infra`

`infra.evm.broadcast` codifica lo que hay que firmar; estos son de sólo lectura y
específicos de los contratos de V3 —`getPool`, `quoteExactInputSingle`, `slot0`—
así que se quedan con el motor que los usa. Leer no mueve dinero y no necesita
estar en el camino que firma.

### La misma historia en el multi-salto, y un camino que no es una lista

El swap de varios pools repite la división: `exactInput` del V1 lleva `deadline`
(`0xc04b8d59`) y el del SwapRouter02 no (`0xb858183f`), y el QuoterV2 cotiza el
camino entero con `quoteExactInput(bytes,uint256)` (`0xcdca1753`). Los tres
selectores se midieron el 2026-10-09 buscándolos en el bytecode desplegado: los
del V1 y el QuoterV2 en Polygon, los del SwapRouter02 y su quoter en Base y BSC.

Y el `bytes path` no es una lista de direcciones como el `path[]` de V2: es el
camino **empaquetado** —token de 20 bytes, comisión del tramo en 3 bytes, token
siguiente— sin longitud por medio. `packed_path` lo construye y valida su forma:
un camino con los bytes desalineados no revierte al codificarlo, revierte en la
cadena, después de firmar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from amigocompora.domain.errors import EngineConfigError, SourceResponseError
from amigocompora.infra.evm.broadcast import selector, word_address, word_uint

#: `exactInputSingle` del SwapRouter V1: ocho campos, con `deadline`.
EXACT_INPUT_SINGLE_V1: Final = (
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
)

#: `exactInputSingle` del SwapRouter02: siete campos, **sin** `deadline`.
EXACT_INPUT_SINGLE_SR02: Final = (
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
)

#: `exactInput` del SwapRouter V1: el struct del multi-salto **con** `deadline`.
EXACT_INPUT_V1: Final = "exactInput((bytes,address,uint256,uint256,uint256))"

#: `exactInput` del SwapRouter02: el mismo struct **sin** `deadline`.
EXACT_INPUT_SR02: Final = "exactInput((bytes,address,uint256,uint256))"

MULTICALL: Final = "multicall(bytes[])"
UNWRAP_WETH9: Final = "unwrapWETH9(uint256,address)"
REFUND_ETH: Final = "refundETH()"
GET_POOL: Final = "getPool(address,address,uint24)"
SLOT0: Final = "slot0()"
TOKEN0: Final = "token0()"
QUOTE_EXACT_INPUT_SINGLE: Final = (
    "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)
QUOTE_EXACT_INPUT: Final = "quoteExactInput(bytes,uint256)"

SELECTOR_EXACT_INPUT_SINGLE_V1: Final = selector(EXACT_INPUT_SINGLE_V1)
SELECTOR_EXACT_INPUT_SINGLE_SR02: Final = selector(EXACT_INPUT_SINGLE_SR02)
SELECTOR_EXACT_INPUT_V1: Final = selector(EXACT_INPUT_V1)
SELECTOR_EXACT_INPUT_SR02: Final = selector(EXACT_INPUT_SR02)
SELECTOR_MULTICALL: Final = selector(MULTICALL)
SELECTOR_UNWRAP_WETH9: Final = selector(UNWRAP_WETH9)
SELECTOR_REFUND_ETH: Final = selector(REFUND_ETH)
SELECTOR_GET_POOL: Final = selector(GET_POOL)
SELECTOR_SLOT0: Final = selector(SLOT0)
SELECTOR_TOKEN0: Final = selector(TOKEN0)
SELECTOR_QUOTE_EXACT_INPUT_SINGLE: Final = selector(QUOTE_EXACT_INPUT_SINGLE)
SELECTOR_QUOTE_EXACT_INPUT: Final = selector(QUOTE_EXACT_INPUT)

#: Lo que se **midió**: cada selector buscado literalmente dentro del bytecode
#: desplegado, que es donde el dispatcher de Solidity lo lleva. Un contrato que
#: tiene `0x1698ee82` en su dispatcher es un contrato con `getPool`.
#:
#: Las direcciones con las que se midió están en `addresses.DEPLOYMENTS`: el
#: SwapRouter V1 en Ethereum, Optimism, Arbitrum y Polygon, y el SwapRouter02 en
#: las seis redes. Medido el 2026-10-06.
#:
#: Los del multi-salto se midieron el 2026-10-09 con la misma técnica y además
#: **en funcionamiento**: `quoteExactInput` con el camino empaquetado
#: WPOL→USDC→pUSD devolvió en Polygon exactamente lo que componen sus dos patas
#: sueltas (99594 unidades de 6 decimales por 1 POL), que es lo que demuestra
#: que el camino empaquetado que se codifica es el que el contrato espera. Y el
#: `exactInput` codificado se llamó contra el router V1 desplegado: revierte en
#: el `transferFrom` del token (`STF`), o sea que el contrato decodificó el
#: struct entero y sólo echó en falta los fondos; con el desplazamiento interno
#: de `bytes` corrompido el revert es otro —`slice_outOfBounds`—, que es la
#: prueba de que la forma codificada es la que se decodifica.
_MEASURED: Final[Mapping[str, str]] = {
    EXACT_INPUT_SINGLE_V1: "0x414bf389",
    EXACT_INPUT_SINGLE_SR02: "0x04e45aaf",
    EXACT_INPUT_V1: "0xc04b8d59",
    EXACT_INPUT_SR02: "0xb858183f",
    MULTICALL: "0xac9650d8",
    UNWRAP_WETH9: "0x49404b7c",
    REFUND_ETH: "0x12210e8a",
    GET_POOL: "0x1698ee82",
    TOKEN0: "0x0dfe1681",
    QUOTE_EXACT_INPUT_SINGLE: "0xc6a5026a",
    QUOTE_EXACT_INPUT: "0xcdca1753",
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
            "la ABI de Uniswap V3 no coincide con la que se midió en la cadena: "
            + "; ".join(wrong)
            + ". No se importa el motor: un selector desviado llama a otra "
            "función del mismo contrato y no se nota hasta que hay fondos dentro."
        )


_require_measured_selectors()


# --------------------------------------------------------------------------- #
# Codificación
# --------------------------------------------------------------------------- #
def exact_input_single(
    *,
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    amount_in_raw: int,
    amount_out_min_raw: int,
    deadline: int | None = None,
) -> str:
    """`exactInputSingle` en la forma que espera el router de esa red.

    `deadline` es obligatorio en V1 y **no existe** en SwapRouter02, así que su
    presencia decide qué codificador se usa. No es un parámetro opcional por
    comodidad: pasar el de V1 a un SwapRouter02 produce un calldata con un campo
    de más que el contrato leería como otro distinto.

    `sqrtPriceLimitX96` va a cero, que es el valor que significa «sin límite de
    precio»: el límite efectivo lo pone `amount_out_min`, que es lo que el
    usuario ha visto y lo que el payload garantiza.
    """
    head = (
        word_address(token_in)
        + word_address(token_out)
        + word_uint(fee)
        + word_address(recipient)
    )
    tail = word_uint(amount_in_raw) + word_uint(amount_out_min_raw) + word_uint(0)
    if deadline is None:
        return SELECTOR_EXACT_INPUT_SINGLE_SR02 + head + tail
    return SELECTOR_EXACT_INPUT_SINGLE_V1 + head + word_uint(deadline) + tail


def packed_path(tokens: Sequence[str], fees: Sequence[int]) -> str:
    """El camino empaquetado del multi-salto: `token(20) ‖ comisión(3) ‖ …`.

    No es una lista de direcciones como el `path[]` de V2: son los bytes
    pegados, sin longitud por medio. De `n` comisiones salen `n + 1` tokens, y
    la forma —20 bytes de token y 23 por tramo— se valida aquí y no se supone:
    un camino con los bytes desalineados no falla al codificarlo, falla en la
    cadena después de firmar.

    La comisión va en **centésimas de punto básico** (el tramo `3000` es el
    0,30 %), que es la unidad en la que la fábrica construyó el pool: es la
    misma regla que en el resto del módulo, y el sitio donde equivocarla
    construiría el camino contra otro pool sin que nada chirriara.
    """
    if len(tokens) != len(fees) + 1:
        raise SourceResponseError(
            f"un camino de {len(tokens)} tokens lleva {len(tokens) - 1} comisiones "
            f"y llegaron {len(fees)}: el camino no encadena"
        )
    parts: list[str] = []
    for index, token in enumerate(tokens):
        parts.append(_address_body(token))
        if index < len(fees):
            fee = fees[index]
            if not 0 < fee < 1 << 24:
                raise SourceResponseError(
                    f"la comisión de un tramo ocupa 3 bytes: {fee} no cabe como "
                    f"tramo de V3"
                )
            parts.append(f"{fee:06x}")
    return "0x" + "".join(parts)


def exact_input(
    *,
    path: str,
    recipient: str,
    amount_in_raw: int,
    amount_out_min_raw: int,
    deadline: int | None = None,
) -> str:
    """`exactInput` en la forma que espera el router de esa red, con el camino dado.

    Igual que `exact_input_single`, el `deadline` decide el codificador: el
    struct del V1 lleva cinco campos y el del SwapRouter02 cuatro, así que
    pasarle a uno la forma del otro es llamar a otra función. La diferencia con
    el de un solo pool es el primer campo —el camino empaquetado en vez de
    tokenIn/tokenOut/fee— y que aquí **no hay `sqrtPriceLimitX96`**: el límite
    de precio no existe en el multi-salto, y el efectivo lo pone igualmente
    `amount_out_min`.
    """
    body = _hex_body(path)
    _require_packed_path(body)
    dynamic = word_uint(len(body) // 2) + body + _padding(len(body))
    if deadline is None:
        head = (
            word_uint(0x80)
            + word_address(recipient)
            + word_uint(amount_in_raw)
            + word_uint(amount_out_min_raw)
        )
        return SELECTOR_EXACT_INPUT_SR02 + word_uint(32) + head + dynamic
    head = (
        word_uint(0xA0)
        + word_address(recipient)
        + word_uint(deadline)
        + word_uint(amount_in_raw)
        + word_uint(amount_out_min_raw)
    )
    return SELECTOR_EXACT_INPUT_V1 + word_uint(32) + head + dynamic


def multicall(payloads: Sequence[str]) -> str:
    """`multicall(bytes[])` con los payloads dados, en orden.

    Se usa para las dos cosas que el router no puede hacer en una sola llamada:

    - **Devolver el sobrante.** Cuando se manda el nativo, el router cobra lo que
      consume y el resto se queda en el contrato si nadie lo reclama; por eso el
      patrón es `multicall([swap, refundETH])`. Aquí `amount_in` es exacto y el
      sobrante sería cero, pero el reembolso se incluye igual: el día que el
      router cobre menos de lo previsto, la diferencia vuelve al usuario en vez
      de quedarse en un contrato ajeno.
    - **Desenvolver.** Un swap que termina en el nativo no puede recibirlo: V3
      entrega WETH. Se pide que el router se lo mande a sí mismo y luego se llama
      a `unwrapWETH9`, que lo retira y lo transfiere al destinatario real.

    La codificación de `bytes[]` es la estándar de la ABI: una palabra con el
    desplazamiento del array, la longitud, la tabla de desplazamientos de cada
    elemento —relativos al principio de esa tabla, no al del calldata— y cada
    elemento con su longitud y su contenido relleno a 32 bytes.

    El avance de un elemento al siguiente es **su tamaño ya rellenado**, no el
    tamaño de su contenido. `refundETH()` son 4 bytes de contenido y 32 de
    elemento: contar 36 pone el segundo desplazamiento 28 bytes antes de donde
    el decodificador busca el elemento, y el router lee basura como longitud.
    Con un solo elemento no se nota —no hay siguiente—, y estos `multicall`
    llevan dos o tres, así que se notaría en cada swap con el nativo.
    """
    if not payloads:
        raise SourceResponseError(
            "un multicall sin llamadas no tiene sentido: el router no haría nada"
        )
    bodies = [_hex_body(payload) for payload in payloads]
    count = len(bodies)
    offsets: list[int] = []
    cursor = count * 32
    for body in bodies:
        offsets.append(cursor)
        cursor += 32 + _padded_bytes(len(body))
    array = (
        word_uint(count)
        + "".join(word_uint(offset) for offset in offsets)
        + "".join(word_uint(len(body) // 2) + body + _padding(len(body)) for body in bodies)
    )
    return SELECTOR_MULTICALL + word_uint(32) + array


def unwrap_weth9(amount_min_raw: int, recipient: str) -> str:
    """`unwrapWETH9(amountMinimum, recipient)`: saca el nativo del envoltorio."""
    return SELECTOR_UNWRAP_WETH9 + word_uint(amount_min_raw) + word_address(recipient)


def refund_eth() -> str:
    """`refundETH()`: devuelve al pagador el nativo que el router no gastó."""
    return SELECTOR_REFUND_ETH


def get_pool(token_a: str, token_b: str, fee: int) -> str:
    """`getPool(tokenA, tokenB, fee)`, para un `eth_call` a la fábrica.

    El orden de los tokens no importa: la fábrica ordena por dirección antes de
    calcular la del pool, así que `(A, B)` y `(B, A)` dan el mismo contrato.
    """
    return SELECTOR_GET_POOL + word_address(token_a) + word_address(token_b) + word_uint(fee)


def quote_exact_input_single(
    token_in: str,
    token_out: str,
    amount_in_raw: int,
    fee: int,
) -> str:
    """`quoteExactInputSingle` del QuoterV2, para un `eth_call`.

    El orden de los campos es `(tokenIn, tokenOut, amountIn, fee,
    sqrtPriceLimitX96)` — **la comisión va después del importe**, al contrario
    que en la struct del swap, donde va tercera. Se codifica leyendo la firma de
    arriba, no de memoria: intercambiar esas dos palabras no falla, cotiza otro
    pool.
    """
    return (
        SELECTOR_QUOTE_EXACT_INPUT_SINGLE
        + word_address(token_in)
        + word_address(token_out)
        + word_uint(amount_in_raw)
        + word_uint(fee)
        + word_uint(0)
    )


def quote_exact_input(path: str, amount_in_raw: int) -> str:
    """`quoteExactInput` del QuoterV2, para un `eth_call`, con el camino entero.

    Cotiza el multi-salto de una vez, **como se ejecutará**: es el mismo camino
    empaquetado que lleva el swap, así que el número no es la composición de
    dos lecturas sino el que da el contrato para esta ruta. En el retorno, la
    primera palabra es el importe de salida; las listas que vienen detrás —los
    precios tras cada tramo y los ticks cruzados— no se usan aquí, y el impacto
    se mide contra el marginal de cada pool, como en el camino de un solo pool.
    """
    body = _hex_body(path)
    _require_packed_path(body)
    return (
        SELECTOR_QUOTE_EXACT_INPUT
        + word_uint(64)
        + word_uint(amount_in_raw)
        + word_uint(len(body) // 2)
        + body
        + _padding(len(body))
    )


def slot0() -> str:
    """`slot0()` del pool, para un `eth_call`."""
    return SELECTOR_SLOT0


def token0() -> str:
    """`token0()` del pool: cuál de los dos tokens abre el par.

    Se lee en vez de deducirse. La fábrica ordena los tokens por dirección al
    crear el pool, así que comparar direcciones daría lo mismo hoy; leerlo lo
    sigue dando si esa regla cambiara, y una comparación invertida produciría un
    impacto de precio con el signo cambiado sin que nada chirriara.
    """
    return SELECTOR_TOKEN0


# --------------------------------------------------------------------------- #
# Decodificación
# --------------------------------------------------------------------------- #
def decode_address(raw: str, index: int = 0) -> str | None:
    """La dirección de la palabra `index`, o `None` si la respuesta no llega.

    `None` y no una excepción porque el caso normal de `getPool` es que **no haya
    pool** en ese tramo, y la fábrica contesta con la dirección cero. Distinguir
    «no hay pool» de «la respuesta está rota» es trabajo de quien llama.
    """
    word = _word(raw, index)
    return None if word is None else "0x" + word[24:]


def decode_uint(raw: str, index: int = 0) -> int | None:
    """El entero de la palabra `index`, o `None` si la respuesta no llega."""
    word = _word(raw, index)
    return None if word is None else int(word, 16)


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


def _hex_body(value: str) -> str:
    """El cuerpo hexadecimal de un calldata, sin `0x` y validado."""
    text = value.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    if not text or len(text) % 2 != 0:
        raise SourceResponseError(
            f"«{value[:20]}…» no es un calldata hexadecimal de longitud par"
        )
    try:
        bytes.fromhex(text)
    except ValueError as error:
        raise SourceResponseError(f"«{value[:20]}…» no es hexadecimal") from error
    return text


def _padding(body_hex_length: int) -> str:
    """El relleno del último bloque de 32 bytes de un elemento dinámico."""
    return "0" * ((-body_hex_length) % 64)


def _address_body(address: str) -> str:
    """Una dirección como los 20 bytes exactos del camino empaquetado, validada.

    La comprobación de longitud no es ceremonia: pegar una dirección de 19 o 21
    bytes corre **todos** los bytes siguientes del camino y el contrato leería
    una comisión donde hay media dirección, sin revertir al codificar.
    """
    body = address.strip().lower().removeprefix("0x")
    if len(body) != 40:
        raise SourceResponseError(
            f"una dirección del camino empaquetado ocupa 20 bytes: «{address}» "
            f"no es una"
        )
    try:
        bytes.fromhex(body)
    except ValueError as error:
        raise SourceResponseError(f"«{address}» no es hexadecimal") from error
    return body


def _require_packed_path(body: str) -> None:
    """Comprueba la forma del camino empaquetado: `20 + 23·k` bytes.

    Se valida en los dos sitios que lo consumen —la cotización y el swap— y no
    en `packed_path` solamente, porque un camino puede llegar construido de
    fuera. La regla es exactamente la del contrato: token de 20 bytes y tramo de
    3, sin longitud por medio.
    """
    if len(body) < 86 or (len(body) - 40) % 46 != 0:
        raise SourceResponseError(
            f"un camino empaquetado de V3 mide 20 + 23·k bytes —un token de 20 y "
            f"una comisión de 3 por tramo— y llegaron {len(body) // 2}: la forma "
            f"equivocada no revierte al codificarla, revierte en la cadena"
        )


def _padded_bytes(body_hex_length: int) -> int:
    """Cuánto ocupa un elemento ya relleno, en bytes.

    Es lo que hay que avanzar para llegar al siguiente: el relleno forma parte
    del elemento, y saltárselo deja el desplazamiento siguiente por detrás de
    donde el decodificador lo busca.
    """
    return (body_hex_length + len(_padding(body_hex_length))) // 2
