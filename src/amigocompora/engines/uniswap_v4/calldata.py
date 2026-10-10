"""La ABI de Uniswap V4 que este motor usa, con los selectores **comprobados**.

### Un pool de V4 no es un contrato, es una clave

En V3 un pool tiene dirección y se le puede preguntar a la fábrica cuál es. En V4
no hay fábrica ni dirección: un pool se identifica por su `PoolKey`
—`(currency0, currency1, fee, tickSpacing, hooks)`— y su `poolId` es el
`keccak256` de esas cinco palabras concatenadas. No hay a quién preguntarle «¿cuál
es la dirección del pool?»: o se deriva bien, o se lee el estado de un pool que no
existe.

Dos consecuencias que no son evidentes:

- **`currency0` tiene que ser la dirección numéricamente menor.** Si se invierte el
  orden, el `keccak256` da otro `poolId` —el de un pool distinto, que puede no
  existir— y el motor cotizaría contra la nada sin que nada chirriara. Por eso el
  orden **y el sentido del intercambio** los deriva `orient` en un solo sitio, y
  tanto la cotización como el swap pasan por ahí: son la misma decisión vista dos
  veces, y tomarlas por separado es como se acaba cotizando en un pool y
  ejecutando en otro. Medido: pasarle al cotizador el par sin ordenar devuelve
  `PoolNotInitialized()`, envolviendo el revert que lanza el `PoolManager`.
- **El nativo es una moneda de pleno derecho.** Se representa como la dirección
  cero y el PoolManager lo liquida con `msg.value`, no moviendo un ERC-20. Ésa es
  la diferencia de fondo con V3, donde el nativo se envuelve siempre: en V4 hay
  pools de ETH nativo —**medidos, en las siete redes**— y pagar con ETH usa ese
  pool, no el de WETH.

### La trampa que costó el bloqueo del cotizador

Un parámetro de tipo struct que lleva un `bytes` dentro es **dinámico**, y entonces
la codificación empieza con la palabra de desplazamiento a su propia cabeza. Sin
ella el decodificador del contrato lee el primer campo donde está el
desplazamiento y revierte **sin dato** — que es como revierte todo lo que no
decodifica, y que no se distingue de un pool vacío ni de un nodo roto.

Son **tres** sitios con la misma regla, y los tres se equivocan igual:

1. `quoteExactInputSingle`: `QuoteExactSingleParams` lleva `bytes hookData`.
2. `execute`: `commands` es un `bytes` y `inputs` un `bytes[]`, así que la cabeza
   de la función son sus dos desplazamientos más el `deadline`.
3. `V4_SWAP`: su carga es `abi.encode(bytes actions, bytes[] params)`, otros dos.

Las tres se midieron contra la cadena con controles que separan «decodifica» de
«revierte», no mirando si «funciona»: una llamada con una acción inexistente tiene
que devolver `UnsupportedAction`, y una con sólo el intercambio tiene que devolver
`CurrencyNotSettled` — si devuelven un revert seco, no es que falte liquidez, es
que el calldata no llega.

### Las acciones no son selectores, y ahí se coló un error

Las acciones de V4 (`SWAP_EXACT_IN_SINGLE`, `SETTLE_ALL`, …) **no son funciones**:
son un enumerado que viaja empaquetado dentro de un `bytes`. Por eso no aparecen
en el dispatcher y no se pueden medir escaneando `PUSH4` como los selectores.

La tabla que se llevaba de memoria tenía `SETTLE_ALL = 0x0b` y `TAKE_ALL = 0x0c`.
Leída del `Actions.sol` **verificado**, la de verdad es `SETTLE_ALL = 0x0c` y
`TAKE_ALL = 0x0f`; el `0x0b` es `SETTLE` a secas, que cobra de un pagador que se le
indica, y el `0x0c` no es `TAKE_ALL`. Las dos estaban corridas una posición: con
esos números se habría llamado a **otras acciones**, sin error de compilación y sin
fallo hasta el `revert`.

Los selectores sí se calculan y se contrastan, como en V3: `_MEASURED` guarda lo
que se buscó literalmente en el bytecode desplegado y `_require_measured_selectors`
corta el import si alguno se desvía. Fallar al importar es el sitio barato.

### Los decodificadores viven aquí

`infra.evm.broadcast` codifica lo que hay que firmar; estos son de sólo lectura y
específicos de los contratos de V4 —`getSlot0`, `getLiquidity`,
`quoteExactInputSingle`—, así que se quedan con el motor que los usa. Leer no mueve
dinero y no necesita estar en el camino que firma.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from eth_utils.crypto import keccak

from amigocompora.domain.errors import EngineConfigError, SourceResponseError
from amigocompora.infra.evm.broadcast import selector, word_address, word_uint

#: `quoteExactInputSingle` del V4Quoter. Su parámetro es una struct con un `bytes`
#: dentro, así que es dinámico: ver el docstring del módulo.
QUOTE_EXACT_INPUT_SINGLE: Final = (
    "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
)

#: `quoteExactInput` del V4Quoter: un **camino** entero en una sola llamada. La
#: firma se midió en el dispatcher del bytecode desplegado, no se dedujo: la que
#: se llevaba de memoria —`quoteExactInput(bytes,uint256)`— **no existe** en el
#: contrato (su selector `0xcdca1753` no aparece por ningún lado), y la real
#: lleva una struct con dos campos dinámicos dentro. Escribir la de memoria
#: habría llamado a otra función o a ninguna, sin error hasta el revert.
QUOTE_EXACT_INPUT: Final = (
    "quoteExactInput((address,(address,uint24,int24,address,bytes)[],uint128))"
)

#: `StateView`. Son las dos preguntas que en V3 le hacía la fábrica: «¿existe este
#: pool?» y «¿tiene liquidez?». Aquí no hay fábrica, así que se le preguntan al
#: estado por el `poolId` derivado.
GET_SLOT0: Final = "getSlot0(bytes32)"
GET_LIQUIDITY: Final = "getLiquidity(bytes32)"

#: El Universal Router. Es el único punto de entrada de un swap de V4: a
#: diferencia de V3, que tiene un router por red y dos formas de cobrar, aquí hay
#: **un solo** contrato que despacha por comandos y que además lleva el `deadline`
#: — así que la caducidad del payload existe en todas las redes, no sólo donde el
#: router de V3 la tiene en su struct.
EXECUTE: Final = "execute(bytes,bytes[],uint256)"

SELECTOR_QUOTE_EXACT_INPUT_SINGLE: Final = selector(QUOTE_EXACT_INPUT_SINGLE)
SELECTOR_QUOTE_EXACT_INPUT: Final = selector(QUOTE_EXACT_INPUT)
SELECTOR_GET_SLOT0: Final = selector(GET_SLOT0)
SELECTOR_GET_LIQUIDITY: Final = selector(GET_LIQUIDITY)
SELECTOR_EXECUTE: Final = selector(EXECUTE)

# --------------------------------------------------------------------------- #
# Acciones y comandos, leídos del `Actions.sol` y el `Commands.sol` verificados
# --------------------------------------------------------------------------- #
#: Se intercambia contra el pool y se anotan la deuda y el saldo a favor.
ACTION_SWAP_EXACT_IN_SINGLE: Final = 0x06
#: Se intercambia contra un **camino** de varios pools, con la misma protección
#: de precio dentro (`ExactInputParams` en vez de `ExactInputSingleParams`). Se
#: midió con el mismo control que la de un solo pool: con el envoltorio bien
#: formado el contrato **decodifica** y llega hasta liquidar —devuelve
#: `CurrencyNotSettled()`, el mismo revert que el control de un salto—, y sin la
#: palabra de desplazamiento del struct revierte **sin dato**. Decodificar y
#: revertir son la misma respuesta para el nodo; lo que los separa es a qué
#: acción entró: una acción inexistente devolvería `UnsupportedAction`.
ACTION_SWAP_EXACT_IN: Final = 0x07
#: Liquida **toda** la deuda de esa moneda. El importe no se le indica: lo
#: pregunta el propio contrato al PoolManager, así que no se puede pagar de menos
#: por un redondeo nuestro.
ACTION_SETTLE_ALL: Final = 0x0C
#: Cobra el saldo a favor de esa moneda y lo entrega a **un destinatario que se le
#: indica**. Se prefiere a `TAKE_ALL` (0x0f) aunque haga lo mismo, y el motivo está
#: medido: `TAKE_ALL` entrega a `msgSender()` —quien llamó a `execute`, o sea el
#: firmante— y no lleva destinatario, así que con él el payload no podría cumplir
#: el destinatario que declara. El mínimo que `TAKE_ALL` comprobaría sobre el saldo
#: lo comprueba igualmente `SWAP_EXACT_IN_SINGLE` sobre el importe del pool, que
#: tras un solo intercambio es el mismo número.
ACTION_TAKE: Final = 0x0E
#: El comando que abre la puerta a las acciones de arriba.
COMMAND_V4_SWAP: Final = 0x10

#: El cero, en el campo de importe de `SETTLE` y `TAKE`, **no significa cero**:
#: significa «el importe completo», que el contrato pregunta al PoolManager. Es la
#: constante `OPEN_DELTA` de `ActionConstants`. Se nombra porque escribir `0` en
#: una función que se llama `take` se lee como «no cobres nada».
OPEN_DELTA: Final = 0

#: Dos direcciones que en el campo de destinatario **no son direcciones**: son
#: centinelas que el contrato traduce antes de usarlas —`MSG_SENDER` = 1 pasa a ser
#: quien firma, y `ADDRESS_THIS` = 2 pasa a ser el propio router—. Se rechazan al
#: codificar porque ninguno de los dos es lo que se quiso decir: el primero manda
#: los fondos a un sitio distinto del declarado y el segundo los deja dentro del
#: router. Quien valida una dirección aguas arriba no puede saber esto, porque
#: `0x…01` es una dirección válida como cualquier otra.
_RECIPIENT_SENTINELS: Final[frozenset[str]] = frozenset(
    {
        "0x" + "00" * 19 + "01",
        "0x" + "00" * 19 + "02",
    }
)

#: Sin hooks. La dirección cero en el `PoolKey` es un pool sin ganchos, que es
#: donde está toda la liquidez medida: un pool con hooks es otro `poolId` y no se
#: sondea, porque ejecutar sus ganchos puede hacer cualquier cosa y no se puede
#: prometer nada sobre el resultado.
NO_HOOKS: Final = "0x" + "00" * 20

#: Lo que se **midió**: cada selector buscado literalmente dentro del bytecode
#: desplegado, que es donde el dispatcher de Solidity lo lleva.
#:
#: Medido el 2026-10-06 contra los contratos de la tabla de `addresses`, y el
#: del camino el 2026-10-09 contra el mismo V4Quoter de Base —los catorce
#: `PUSH4` de su dispatcher, uno a uno—. Las acciones de arriba **no** están
#: aquí a propósito: no son funciones y no aparecen en ningún dispatcher.
_MEASURED: Final[Mapping[str, str]] = {
    QUOTE_EXACT_INPUT_SINGLE: "0xaa9d21cb",
    QUOTE_EXACT_INPUT: "0xca253dc9",
    GET_SLOT0: "0xc815641c",
    GET_LIQUIDITY: "0xfa6793d5",
    EXECUTE: "0x3593564c",
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
            "la ABI de Uniswap V4 no coincide con la que se midió en la cadena: "
            + "; ".join(wrong)
            + ". No se importa el motor: un selector desviado llama a otra "
            "función del mismo contrato y no se nota hasta que hay fondos dentro."
        )


_require_measured_selectors()


# --------------------------------------------------------------------------- #
# Derivación del pool
# --------------------------------------------------------------------------- #
def sorted_currencies(a: str, b: str) -> tuple[str, str]:
    """Las dos monedas en el orden canónico del `PoolKey`: la menor primero.

    El orden no es una preferencia: **es** el `poolId`. El `keccak256` de
    `(A, B, …)` y el de `(B, A, …)` son dos números distintos, así que invertirlo
    no da un error, da otro pool —normalmente uno que no existe—. Por eso se
    ordena en un solo sitio y todo lo demás lo usa.
    """
    low_a, low_b = a.lower(), b.lower()
    return (low_a, low_b) if int(low_a, 16) < int(low_b, 16) else (low_b, low_a)


def pool_key(
    currency0: str,
    currency1: str,
    fee: int,
    tick_spacing: int,
    hooks: str = NO_HOOKS,
) -> str:
    """`abi.encode(PoolKey)`: las cinco palabras que identifican un pool.

    Cinco campos estáticos y de tamaño fijo, así que no hay tuplas ni
    desplazamientos: es la concatenación y ya. El `int24` del espaciado va con el
    signo extendido —ver `_word_int`—, y los hooks son una dirección, no un
    contrato cualquiera: `word_address` la valida.
    """
    return (
        word_address(currency0)
        + word_address(currency1)
        + word_uint(fee)
        + _word_int(tick_spacing)
        + word_address(hooks)
    )


def pool_id(
    currency0: str,
    currency1: str,
    fee: int,
    tick_spacing: int,
    hooks: str = NO_HOOKS,
) -> str:
    """El `keccak256` del `PoolKey`: el nombre con el que el PoolManager guarda el pool.

    Se ordenan las monedas aquí y no se confía en que quien llama lo haya hecho:
    es la operación en la que equivocarse no da un error, da otro pool.
    """
    c0, c1 = sorted_currencies(currency0, currency1)
    return _keccak(pool_key(c0, c1, fee, tick_spacing, hooks))


@dataclass(frozen=True, slots=True)
class Orientation:
    """Un pool con el sentido del intercambio ya resuelto.

    Existe para que **nadie tenga que acordarse de ordenar**. La clave del pool y
    el sentido del swap son la misma decisión vista dos veces —si `currency0` es la
    que entra, el sentido es `currency0 → currency1`—, y tomarlas por separado es
    como se llega a cotizar en un pool y ejecutar en otro.

    Costó un `PoolNotInitialized()` medido: se llamó al cotizador con
    `(USDC, WETH)` tal cual, sin ordenar, y el `keccak256` salió de un pool que no
    existe. Con el orden derivado aquí, la cotización y el swap **no pueden**
    discrepar, porque los dos leen esta misma estructura.
    """

    #: El `PoolKey` codificado, con `currency0` siendo la dirección menor.
    key: str
    #: Cierto cuando la moneda que entra es `currency0`, que es la única forma en
    #: que el PoolManager acepta el sentido.
    zero_for_one: bool


def orient(
    currency_in: str,
    currency_out: str,
    fee: int,
    tick_spacing: int,
    hooks: str = NO_HOOKS,
) -> Orientation:
    """Ordena el par, deriva la clave y resuelve el sentido del intercambio.

    Es el único sitio del motor que decide cuál de las dos monedas es `currency0`.
    `currency_in` y `currency_out` son lo que se tiene y lo que se quiere —en ese
    orden, que es como se lee un intercambio—, y el orden canónico del `PoolKey` se
    deriva del valor de las direcciones, no de cómo se hayan escrito los argumentos.
    """
    currency0, currency1 = sorted_currencies(currency_in, currency_out)
    return Orientation(
        key=pool_key(currency0, currency1, fee, tick_spacing, hooks),
        zero_for_one=currency0 == currency_in.lower(),
    )


def _keccak(palabras_hex: str) -> str:
    """El `keccak256` de unas palabras ABI ya codificadas, en hexadecimal."""
    return "0x" + keccak(bytes.fromhex(palabras_hex)).hex()


# --------------------------------------------------------------------------- #
# El camino — `PathKey[]`
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PathKey:
    """Un tramo de un camino de V4: **hasta** dónde llega, y por qué pool.

    Un camino de V4 no se escribe como una lista de tokens con la comisión
    intercalada, como el `path` empaquetado de V3, ni como una lista de
    direcciones como la de V2: cada salto se describe entero con su propia
    clave. La diferencia que importa y que costó medir está en `currency`: el
    contrato empareja cada `PathKey` con la moneda que traía para derivar el
    pool del salto —`(currencyIn, pathKey.intermediateCurrency)`—, así que
    `currency` es la moneda a la que **llega** el tramo, no de la que sale.
    Escribirla al revés derivaría la clave de otro pool —normalmente uno que no
    existe— y el único aviso sería un revert del `PoolManager` que no dice
    nada de esto.

    El espaciado de ticks tampoco es decorativo: va **dentro** de la clave del
    pool, así que un tramo con otro espaciado es otro pool, y aquí no hay
    fábrica que lo confirme. El motor lo recupera de la tabla de tramos
    canónicos que sondea, que es la única fuente que tiene.
    """

    #: La moneda a la que **llega** este tramo, no la que entra en él.
    currency: str
    #: El tramo de comisión, en unidades de V4 (centésimas de punto básico).
    fee: int
    #: El espaciado de ticks del pool, que va dentro de su clave.
    tick_spacing: int
    #: Sin ganchos, que es donde está toda la liquidez medida: un pool con
    #: ganchos es otro `poolId` y no se sondea.
    hooks: str = NO_HOOKS
    #: El dato que se le pasa al gancho; vacío cuando no hay ganchos.
    hook_data: str = "0x"


def path_keys(keys: Sequence[PathKey]) -> str:
    """`abi.encode(PathKey[])`: el camino entero, con sus desplazamientos.

    Es un array de structs **dinámicos** —cada `PathKey` lleva un `bytes`
    dentro—, así que no basta con concatenar los elementos: primero la
    longitud, luego un desplazamiento por elemento y detrás los elementos. Los
    desplazamientos se cuentan desde el final de la palabra de longitud y
    avanzan por el tamaño **ya relleno** de cada elemento, igual que en
    `_enc_bytes_array`.

    Dentro de cada elemento la cabeza son cinco palabras —la moneda, la
    comisión, el espaciado, los ganchos y el desplazamiento al `hookData`—, y
    ese desplazamiento vale la cabeza entera: `0xa0`, cinco palabras. Con el
    `hookData` vacío el elemento mide **seis** palabras, y el `int24` del
    espaciado va con el signo extendido, como en el `PoolKey`.

    Un camino vacío no se codifica: el contrato lo cotizaría como un camino de
    cero saltos, que no es un swap. Se corta aquí, que es barato.
    """
    if not keys:
        raise SourceResponseError(
            "un camino sin tramos no es un camino: no hay ningún pool contra el "
            "que intercambiar."
        )
    elementos: list[str] = []
    for key in keys:
        hook = _hex_body(key.hook_data)
        elementos.append(
            word_address(key.currency)
            + word_uint(key.fee)
            + _word_int(key.tick_spacing)
            + word_address(key.hooks)
            + word_uint(5 * 32)
            + _enc_bytes(hook)
        )
    desplazamientos: list[int] = []
    cursor = len(keys) * 32
    for elemento in elementos:
        desplazamientos.append(cursor)
        # Cada elemento ya viene relleno a 32 bytes —la cabeza son cinco
        # palabras y el `bytes` es palabra más relleno—, así que su tamaño en
        # bytes es exactamente lo que hay que avanzar.
        cursor += len(elemento) // 2
    return (
        word_uint(len(keys))
        + "".join(word_uint(offset) for offset in desplazamientos)
        + "".join(elementos)
    )


# --------------------------------------------------------------------------- #
# Codificación — lectura
# --------------------------------------------------------------------------- #
def get_slot0(pool_id_hex: str) -> str:
    """`getSlot0(bytes32)` del StateView, para un `eth_call`.

    Devuelve el estado del pool empaquetado en su `Slot0`: cuatro palabras con el
    precio, el tick y las dos comisiones. Es el precio **de antes** del
    intercambio, que es contra lo que se mide el impacto.
    """
    return SELECTOR_GET_SLOT0 + _hex_word(pool_id_hex)


def get_liquidity(pool_id_hex: str) -> str:
    """`getLiquidity(bytes32)` del StateView, para un `eth_call`.

    Es la pregunta que en V3 le hacía la fábrica y la que aquí sustituye a
    «¿existe el pool?»: **un pool puede estar inicializado y no tener liquidez**.
    Medido en Base: los tramos 0,01 % y 1 % de WETH/USDC tienen precio válido y
    liquidez cero. Cotizarlos revienta sin decir por qué; preguntar por la liquidez
    lo dice con un número, y por eso el filtro es éste y no `sqrtPrice != 0`.
    """
    return SELECTOR_GET_LIQUIDITY + _hex_word(pool_id_hex)


def quote_exact_input_single(
    currency_in: str,
    currency_out: str,
    fee: int,
    tick_spacing: int,
    amount_in_raw: int,
    *,
    hook_data: str = "0x",
) -> str:
    """`quoteExactInputSingle` del V4Quoter, para un `eth_call`.

    La struct va precedida de **su palabra de desplazamiento**, y no es un detalle
    de estilo: al llevar un `bytes` dentro es dinámica, así que el decodificador
    del contrato lee el primer campo donde está el desplazamiento y revierte sin
    dato. Medido: sin esa palabra, `execution reverted data=0x` en tres nodos
    distintos; con ella, el importe exacto.

    Dentro, el `hookData` lleva **otro** desplazamiento relativo al principio de la
    struct, y su valor es el tamaño de la cabeza: **ocho** palabras —las cinco de
    la clave, `zeroForOne`, el importe y el desplazamiento mismo—. Ocho, no nueve:
    `QuoteExactSingleParams` **no** lleva `amountOutMinimum`, que es lo único que
    separa esta struct de la del swap. Confundirlas desplaza el `hookData` una
    palabra y el contrato revierte vacío, sin decir cuál de las dos cosas está mal.

    El orden de las monedas y el sentido del intercambio los resuelve `orient`: se
    le pasa lo que se tiene y lo que se quiere, y no hay forma de que la
    cotización salga de un pool distinto del que se va a ejecutar.

    El importe es un `uint128` y se comprueba: uno que no quepa no cabe en la
    palabra, y truncarlo en silencio cotizaría por otro tamaño.
    """
    _require_quote_amount(amount_in_raw)
    par = orient(currency_in, currency_out, fee, tick_spacing)
    hook = _hex_body(hook_data)
    return (
        SELECTOR_QUOTE_EXACT_INPUT_SINGLE
        + word_uint(32)
        + par.key
        + word_uint(1 if par.zero_for_one else 0)
        + word_uint(amount_in_raw)
        + word_uint(8 * 32)
        + word_uint(len(hook) // 2)
        + hook
        + _padding(len(hook))
    )


def quote_exact_input(
    currency_in: str,
    keys: Sequence[PathKey],
    amount_in_raw: int,
) -> str:
    """`quoteExactInput` del V4Quoter para un camino entero, para un `eth_call`.

    Cotiza todos los saltos **en una sola llamada**, igual que el swap los
    ejecuta: el importe que devuelve no es la composición de varias
    cotizaciones sueltas, es el que el contrato calcula encadenando los pools
    de verdad —cada salto parte de lo que entregó el anterior—, y por eso es el
    número contra el que se comprueba la deriva al firmar.

    Medido contra el V4Quoter de Base, en dos saltos WETH → USDC → WETH
    (tramos 0,30 % y 0,05 %, 10^15 wei de entrada): devuelve
    `[993014236051103, 70983]` —el importe y una estimación de gas—, y la cifra
    cae dentro de 6 partes por millón de la composición de dos cotizaciones
    sueltas del mismo bloque. La diferencia está explicada y no es un error: la
    ruta artificial repite el **mismo** pool en los dos saltos, así que el
    segundo ve el precio que ya movió el primero, mientras que dos
    cotizaciones sueltas parten las dos del estado original.

    El struct va precedido de **su palabra de desplazamiento** por el mismo
    motivo que el de un solo pool: lleva un campo dinámico dentro —el
    `PathKey[]`— y sin esa palabra el decodificador lee la moneda donde está el
    desplazamiento. Medido: sin ella, `execution reverted data=0x` en tres
    nodos distintos; con ella, el importe exacto.

    Dentro, el camino lleva **otro** desplazamiento relativo al principio de la
    struct, y su valor es el tamaño de su cabeza: **tres** palabras —la moneda
    que entra, el desplazamiento y el importe—. Y el importe se comprueba como
    en la variante de un pool: es un `uint128`, y truncarlo cotizaría por otro
    tamaño.
    """
    _require_quote_amount(amount_in_raw)
    return (
        SELECTOR_QUOTE_EXACT_INPUT
        + word_uint(32)
        + word_address(currency_in)
        + word_uint(3 * 32)
        + word_uint(amount_in_raw)
        + path_keys(keys)
    )


def _require_quote_amount(amount_in_raw: int) -> None:
    """El importe de una cotización: positivo y dentro del `uint128`.

    Vive aquí y no en cada variante porque las dos —un pool y un camino—
    declaran el mismo `uint128` y truncarlo del mismo modo: por la izquierda,
    en silencio, cotizando por otro tamaño. El mensaje dice cuál de las dos
    cosas falla, que es lo único que cambia.
    """
    if amount_in_raw <= 0:
        raise SourceResponseError(
            f"no se cotiza un importe de entrada de {amount_in_raw}: tiene que ser "
            f"positivo, y el contrato no admite el cero."
        )
    if amount_in_raw >= 1 << 128:
        raise SourceResponseError(
            f"el importe {amount_in_raw} no cabe en el `uint128` que declara la "
            f"struct del cotizador: el contrato lo leería truncado y cotizaría por "
            f"otro tamaño."
        )


# --------------------------------------------------------------------------- #
# Codificación — escritura
# --------------------------------------------------------------------------- #
def swap_exact_in_single(
    currency_in: str,
    currency_out: str,
    fee: int,
    tick_spacing: int,
    *,
    amount_in_raw: int,
    amount_out_min_raw: int,
    hook_data: str = "0x",
) -> str:
    """`abi.encode(ExactInputSingleParams)`: la primera acción del swap.

    Deriva la clave y el sentido con el mismo `orient` que la cotización, y por eso
    no recibe ni una ni otro: si se pasaran por separado, el swap podría ir a un
    pool distinto del que se cotizó, o en el sentido contrario, y el precio que se
    le enseñó al usuario no sería el que se ejecuta. Es la regla del motor —lo que
    se cotiza y lo que se ejecuta salen de la misma decisión— aplicada donde nace.

    Lleva la palabra de desplazamiento por el mismo motivo que la cotización, y el
    `hookData` va vacío: los pools con ganchos no se sondean, así que no hay nada
    que pasarles.
    """
    par = orient(currency_in, currency_out, fee, tick_spacing)
    hook = _hex_body(hook_data)
    head = 9 * 32  # PoolKey (5) + zeroForOne (1) + amountIn (1) + mínimo (1) + desplazamiento (1)
    return (
        word_uint(32)
        + par.key
        + word_uint(1 if par.zero_for_one else 0)
        + word_uint(amount_in_raw)
        + word_uint(amount_out_min_raw)
        + word_uint(head)
        + word_uint(len(hook) // 2)
        + hook
        + _padding(len(hook))
    )


def swap_exact_in_path(
    currency_in: str,
    keys: Sequence[PathKey],
    *,
    amount_in_raw: int,
    amount_out_min_raw: int,
) -> str:
    """`abi.encode(ExactInputParams)`: la acción que ejecuta un camino entero.

    Es a `SWAP_EXACT_IN` (0x07) lo que `swap_exact_in_single` a su acción: la
    cabeza son **cuatro** palabras —la moneda que entra, el desplazamiento al
    camino, el importe y el mínimo—, y el `PathKey[]` va detrás. Cuatro, no
    cinco: la revisión con la que se desplegó el router
    (`v4-periphery@444c526b77d8`, abril de 2025) **no** lleva el
    `minHopPriceX36` que se le añadió después a la rama principal, y meterlo
    desplazaría el camino una palabra. Se comprobó contra el contrato
    desplegado: con esta cabeza el `UniversalRouter` de Base decodifica la
    acción y llega hasta liquidar —devuelve `CurrencyNotSettled()`, el mismo
    revert que el control de un solo salto—, y sin la palabra de desplazamiento
    revierte **sin dato**.

    El mínimo va aquí dentro igual que en la variante de un pool, y por el
    mismo motivo: es donde el contrato conoce el importe que sale, así que la
    protección de precio viaja en el propio swap y `TAKE` puede cobrar el saldo
    íntegro sin perder nada. Con dos saltos el número se comprueba contra la
    salida del **camino entero**, que es lo que el usuario recibe.
    """
    return (
        word_uint(32)
        + word_address(currency_in)
        + word_uint(4 * 32)
        + word_uint(amount_in_raw)
        + word_uint(amount_out_min_raw)
        + path_keys(keys)
    )


def settle_all(currency: str, max_amount_raw: int) -> str:
    """`abi.encode(Currency, uint256)`: paga toda la deuda de esa moneda.

    Dos campos estáticos, así que no hay desplazamiento. El importe es un **tope**,
    no lo que se paga: el contrato le pregunta al PoolManager cuánto se debe y
    falla si supera este número. Poner el importe exacto que se cree deber es
    correcto y además protege: si el pool cambiara la cuenta, la transacción se
    niega en vez de pagar de más.
    """
    return word_address(currency) + word_uint(max_amount_raw)


def take(currency: str, recipient: str, amount_raw: int = OPEN_DELTA) -> str:
    """`abi.encode(Currency, address, uint256)`: cobra y entrega a un destinatario.

    Los tres campos son estáticos, así que no hay desplazamiento. Con el importe
    en `OPEN_DELTA` cobra el saldo **íntegro**, que es lo que hace `TAKE_ALL` pero
    sin renunciar al destinatario.

    La protección de precio no se pierde por no usar `TAKE_ALL`: la comprobación
    del mínimo que `TAKE_ALL` haría sobre el saldo la hace el propio
    `SWAP_EXACT_IN_SINGLE` sobre el importe que devuelve el pool, que después de un
    solo intercambio es el mismo número. Y se comprueba donde el importe se
    conoce, que es dentro del swap.

    Se rechazan los dos centinelas del campo de destinatario —ver
    `_RECIPIENT_SENTINELS`—: son direcciones válidas para cualquiera que valide
    direcciones y, sin embargo, no significan lo que parecen.
    """
    if recipient.lower() in _RECIPIENT_SENTINELS:
        raise SourceResponseError(
            f"«{recipient}» no es un destinatario en V4: el contrato lo traduce a "
            f"«quien firma» o a «el propio router» en vez de usarlo tal cual, así "
            f"que los fondos irían a un sitio distinto del que declara la orden."
        )
    return word_address(currency) + word_address(recipient) + word_uint(amount_raw)


def v4_swap_input(actions: Sequence[int], params: Sequence[str]) -> str:
    """`abi.encode(bytes actions, bytes[] params)`: la carga del `V4_SWAP`.

    `actions` es un byte por acción, empaquetados sin relleno —el contrato recorre
    `actions.length` y exige que coincida con `params.length`, así que un byte de
    más no se ignora: revienta con `InputLengthMismatch`.

    La cabeza son **dos** desplazamientos, uno por cada parámetro dinámico. Pegar
    el array detrás del `bytes` sin su desplazamiento deja al segundo leyéndose
    donde está el primero, y el contrato revierte sin dato.
    """
    if not actions:
        raise SourceResponseError("un swap sin acciones no haría nada")
    if len(actions) != len(params):
        raise SourceResponseError(
            f"hay {len(actions)} acciones y {len(params)} juegos de parámetros: el "
            f"contrato exige que coincidan y rechaza el calldata entero si no."
        )
    acciones_hex = "".join(f"{action:02x}" for action in actions)
    acciones_enc = _enc_bytes(acciones_hex)
    return (
        word_uint(2 * 32)
        + word_uint(2 * 32 + len(acciones_enc) // 2)
        + acciones_enc
        + _enc_bytes_array(params)
    )


def execute(commands: Sequence[int], inputs: Sequence[str], deadline: int) -> str:
    """`execute(bytes commands, bytes[], uint256 deadline)` del Universal Router.

    La cabeza son **tres** palabras: el desplazamiento de `commands`, el de
    `inputs` y el plazo. Los desplazamientos son obligatorios porque los dos
    primeros parámetros son dinámicos, y omitirlos fue el error que hizo que todo
    revirtiera sin dato — el contrato leía la longitud del `bytes` donde estaba un
    desplazamiento.

    El `deadline` sí existe aquí, en todas las redes: es la protección que en V3
    sólo tenían las redes con SwapRouter V1.
    """
    if len(commands) != len(inputs):
        raise SourceResponseError(
            f"hay {len(commands)} comandos y {len(inputs)} entradas: el contrato "
            f"exige que coincidan y rechaza el calldata entero si no."
        )
    comandos_enc = _enc_bytes("".join(f"{command:02x}" for command in commands))
    cabecera = (
        word_uint(3 * 32)
        + word_uint(3 * 32 + len(comandos_enc) // 2)
        + word_uint(deadline)
    )
    return SELECTOR_EXECUTE + cabecera + comandos_enc + _enc_bytes_array(inputs)


# --------------------------------------------------------------------------- #
# Decodificación
# --------------------------------------------------------------------------- #
def decode_uint(raw: str, index: int = 0) -> int | None:
    """El entero de la palabra `index`, o `None` si la respuesta no llega."""
    word = _word(raw, index)
    return None if word is None else int(word, 16)


def decode_slot0(raw: str) -> tuple[int, int] | None:
    """`(sqrtPriceX96, comisión del LP)` del `Slot0`, o `None` si no llega.

    El `Slot0` vive empaquetado en el PoolManager —`sqrtPriceX96` en los 160 bits
    bajos, luego el tick, la comisión del protocolo y la del LP—, pero el
    `StateView` lo devuelve **desempaquetado en cuatro palabras**, así que aquí se
    leen dos y no hay que hacer máscaras ni desplazamientos de bits.

    La comisión se devuelve para poder contrastarla: el motor la publica como la
    del tramo y descuenta el precio marginal con ella, y si el pool cobrara otra
    cosa esas dos cifras serían mentira sin que nada chirriara. Medido: coincide en
    los cuatro tramos de Base y Ethereum. El `tick` y la comisión del protocolo
    **no** se devuelven porque no se usan: la del protocolo se la queda el pool por
    dentro y ya viene descontada en lo que da el cotizador, y el `tick` viene con
    el signo extendido a los 32 bytes de su palabra —es un `int24` y puede ser
    negativo, medido: los pools de este par están en ticks positivos, pero en un
    par invertido el mismo precio da el tick negativo—, así que leerlo tal cual
    como un entero sin signo daría un número enorme. Quien lo necesite tiene que
    interpretarlo con signo, no leerlo con `decode_uint`.
    """
    sqrt_price = decode_uint(raw, 0)
    lp_fee = decode_uint(raw, 3)
    if sqrt_price is None or lp_fee is None:
        return None
    return sqrt_price, lp_fee


def decode_quote(raw: str) -> int | None:
    """El `amountOut` de la respuesta del V4Quoter, o `None` si no llega.

    Devuelve **dos** palabras —el importe de salida y una estimación de gas— y
    la que interesa es la primera. Que sean dos se midió contra el contrato
    desplegado: el docstring que decía «cinco palabras» describía el retorno
    del `QuoterV2` de V3 —el de este contrato es otro—, y aunque leer la
    primera palabra funcionaba igual, la cifra de gas es un dato distinto del
    que se creía. La de gas es del intercambio solo: el `execute` que lo
    envuelve hace mucho más trabajo, así que usarla como límite de la
    transacción sería quedarse corto por un margen que no se sabe.

    La misma lectura vale para la respuesta de un pool y para la de un camino:
    las dos empiezan por el importe.
    """
    return decode_uint(raw, 0)


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


def _word_int(value: int, bits: int = 24) -> str:
    """Un entero **con signo** en una palabra de 256, extendido como lo haría la ABI.

    `tickSpacing` es un `int24` y puede ser negativo —V4 lo admite, existe el error
    `TickSpacingTooSmall`—, así que se extiende el signo en vez de escribirlo tal
    cual: un negativo sin extender se leería como un número enorme y positivo, y el
    `poolId` saldría de un pool que no es. Se comprueba que quepa en sus bits,
    porque uno que no quepa produciría exactamente ese mismo desastre.
    """
    limite = 1 << (bits - 1)
    if not -limite <= value < limite:
        raise SourceResponseError(
            f"{value} no cabe en un entero con signo de {bits} bits: escribirlo "
            f"truncado daría el número de otro pool."
        )
    return word_uint(value if value >= 0 else value + (1 << 256))


def _hex_word(value: str) -> str:
    """Una palabra de 32 bytes a partir de un hexadecimal de 32 bytes."""
    body = _hex_body(value)
    if len(body) != 64:
        raise SourceResponseError(
            f"«{value[:20]}…» no es una palabra de 32 bytes, y estas funciones "
            f"toman exactamente una: el contrato leería otra cosa."
        )
    return body


def _enc_bytes(cuerpo_hex: str) -> str:
    """Un `bytes` codificado: longitud y contenido relleno a 32 bytes."""
    return word_uint(len(cuerpo_hex) // 2) + cuerpo_hex + _padding(len(cuerpo_hex))


def _enc_bytes_array(elementos: Sequence[str]) -> str:
    """Un `bytes[]` codificado, con los desplazamientos de sus elementos.

    Los desplazamientos se cuentan **desde el final de la palabra de longitud**, no
    desde el principio del array, y el avance de uno al siguiente es el tamaño ya
    relleno —no el del contenido—. Con un solo elemento el segundo detalle no se
    nota; con varios, contar el contenido pone el desplazamiento siguiente por
    detrás de donde el decodificador lo busca.
    """
    count = len(elementos)
    desplazamientos: list[int] = []
    cursor = count * 32
    for elemento in elementos:
        desplazamientos.append(cursor)
        cursor += 32 + _padded_bytes(len(elemento))
    return (
        word_uint(count)
        + "".join(word_uint(offset) for offset in desplazamientos)
        + "".join(_enc_bytes(elemento) for elemento in elementos)
    )


def _hex_body(value: str) -> str:
    """El cuerpo hexadecimal de un calldata, sin `0x` y validado.

    Un calldata **vacío** es legítimo y aquí es lo normal: el `hookData` de un pool
    sin ganchos es un `bytes` de longitud cero, que se codifica como la palabra
    cero y nada más. Exigir que no esté vacío pondría el caso corriente a fallar.
    """
    text = value.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    if len(text) % 2 != 0:
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


def _padded_bytes(body_hex_length: int) -> int:
    """Cuánto ocupa un elemento ya relleno, en bytes.

    Es lo que hay que avanzar para llegar al siguiente: el relleno forma parte del
    elemento, y saltárselo deja el desplazamiento siguiente por detrás de donde el
    decodificador lo busca.
    """
    return (body_hex_length + len(_padding(body_hex_length))) // 2
