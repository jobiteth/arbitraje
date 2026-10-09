"""Leer el estado de la red que falta para firmar, y emitir lo firmado.

`signer.py` firma sin red; este módulo es lo contrario: no sabe de criptografía y
sólo habla JSON-RPC. Entre los dos cubren lo que hace falta para mover dinero, y
la separación no es cosmética — el nonce, el gas y la comisión son estado que
cambia entre el momento en que el usuario mira la pantalla y el momento en que
pulsa «Ejecutar», así que tienen que leerse lo más cerca posible de la firma y no
pueden vivir dentro de una función determinista.

### Lo que este módulo se niega a hacer

**Emitir a ciegas.** Antes de enviar nada, el destino se contrasta contra el que
el motor declaró como medido (`expected_to`). El motor ya lo comprueba al
construir el payload, y se repite aquí a propósito: es la última puerta antes de
una firma irreversible, y una comprobación que sólo existe en el productor del
dato no protege contra el productor del dato.

**Confundir dos fallos que se parecen.** `eth_estimateGas` puede fallar de dos
formas que no significan lo mismo, y el pool ya las distingue:

- el nodo **respondió** con un revert → la transacción revertiría de verdad; no
  se firma, porque firmarla es quemar gas para nada;
- **ningún nodo respondió** → no se pudo preguntar; se usa el `gas_limit` que ya
  traía el payload, o se falla diciendo que falta.

Tratar las dos como «no hay gas» llevaría a firmar transacciones que revierten.

**Dar por fallido un envío que sí entró.** Si el nodo responde `already known`,
la transacción **ya está aceptada**: es la respuesta normal cuando se reintenta el
mismo `raw` tras perderse una respuesta anterior. Esto importa más de lo que
parece: leer ese mensaje como error lleva a firmar otra vez, y con un nonce nuevo
eso sería una segunda operación idéntica — un gasto doble. Aquí se lee como lo
que es, éxito pendiente de mina.

### Los selectores se calculan, no se recuerdan

`allowance`, `approve` y compañía se derivan con keccak de su firma al importar.
Escritos a mano serían cuatro constantes que nadie vuelve a comprobar y un error
de un dígito autorizaría al contrato equivocado; calculados, o son correctos o el
módulo no importa.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import structlog
from eth_utils.crypto import keccak

from amigocompora.domain.chains import ChainSpec
from amigocompora.domain.clock import Clock, SystemClock
from amigocompora.domain.errors import (
    BroadcastError,
    ExecutionError,
    InvalidAmountError,
    UnsupportedOperationError,
)
from amigocompora.domain.execution import GasPolicy
from amigocompora.domain.models import (
    BroadcastReceipt,
    BroadcastStatus,
    SignedEvmTransaction,
    UnsignedTransaction,
)
from amigocompora.infra.rpc.pool import (
    AllEndpointsFailedError,
    RpcError,
    RpcPool,
)

_log = structlog.get_logger(__name__)


def selector(signature: str) -> str:
    """Los 4 primeros bytes de keccak de la firma, en hexadecimal con `0x`.

    Público porque lo comparten la emisión y los motores que construyen calldata
    contra un contrato: un selector escrito de memoria es un selector que nadie
    vuelve a comprobar, y el que se equivoca llama a otra función del mismo
    contrato —o revierte— sin que nada lo delate hasta que hay dinero dentro.
    """
    return "0x" + keccak(text=signature)[:4].hex()


SELECTOR_ALLOWANCE: Final = selector("allowance(address,address)")
SELECTOR_APPROVE: Final = selector("approve(address,uint256)")
#: El de sacar un ERC-20 de la cartera. Se calcula con keccak como los demás: un
#: selector escrito de memoria llama a otra función del mismo contrato, y en el
#: caso de una retirada eso es dinero que sale hacia donde no toca.
SELECTOR_TRANSFER: Final = selector("transfer(address,uint256)")
#: El de consultar un saldo ERC-20 antes de retirarlo.
SELECTOR_BALANCE_OF: Final = selector("balanceOf(address)")

#: Los dos del ERC-1155, que es el estándar de las participaciones de un mercado
#: de predicción. `isApprovedForAll` es de sólo lectura y `setApprovalForAll`
#: escribe un booleano, no un importe: el estándar no tiene permiso por
#: cantidad, y por eso el camino que lo concede tiene que decirlo al usuario.
SELECTOR_IS_APPROVED_FOR_ALL: Final = selector("isApprovedForAll(address,address)")
SELECTOR_SET_APPROVAL_FOR_ALL: Final = selector("setApprovalForAll(address,bool)")

#: `Permit2.approve(token, spender, amount, expiration)`. Permit2 no es un router:
#: es el contrato por el que pasan los routers modernos, y tiene su propio
#: permiso, separado del `approve` del ERC-20 y con caducidad propia.
SELECTOR_PERMIT2_APPROVE: Final = selector("approve(address,address,uint160,uint48)")

#: `Permit2.allowance(owner, token, spender)`. **No es** el `allowance` del
#: ERC-20, aunque se llame igual: éste lleva el token como segundo argumento y
#: devuelve tres valores —importe, caducidad y nonce— en vez de uno. Se calcula
#: aparte precisamente porque compartir el selector con el del ERC-20 daría una
#: respuesta de forma distinta y un importe leído de la palabra equivocada.
SELECTOR_PERMIT2_ALLOWANCE: Final = selector("allowance(address,address,address)")

#: Margen sobre el gas estimado. `eth_estimateGas` devuelve el mínimo exacto para
#: el estado *de ahora*; entre la estimación y la mina cambia el almacenamiento
#: del pool, y un límite justo se queda corto y revierte la operación por falta
#: de gas. Un 25 % de holgura no cuesta nada: se paga el gas **usado**, no el
#: límite.
GAS_ESTIMATE_MARGIN: Final = Decimal("1.25")

#: Un `eth_estimateGas` que no se puede leer no debe impedir firmar si el payload
#: ya traía límite; lo que no puede es pasar en silencio.
_UNKNOWN_GAS_SOURCE: Final = "desconocida"

#: Propina mínima cuando no hay forma de leer ninguna de la red, en wei. 0,001
#: gwei: por debajo de esto hay nodos que descartan la transacción de la mempool
#: por «propina demasiado baja», y una transacción descartada es peor que una
#: cara — se queda firmada y sin minar sin que nadie se entere.
MIN_TIP_WEI: Final = 1_000_000

#: Mensajes con los que un nodo dice que **ya tiene** esa transacción. Se
#: comparan en minúsculas y por subcadena porque cada cliente lo redacta a su
#: manera: geth y erigon dicen «already known», besu «Transaction already
#: imported», nethermind «Known transaction». La lista importa: un falso negativo
#: aquí hace creer que el envío falló cuando la operación está viva.
_ALREADY_ACCEPTED: Final = (
    "already known",
    "already imported",
    "known transaction",
    "already exists",
)

#: Los tres estados con los que un recibo dice si la transacción salió bien.
_STATUS_SUCCESS: Final = "0x1"
_STATUS_REVERTED: Final = "0x0"


def _quantity(value: int) -> str:
    """Entero a cantidad JSON-RPC (`0x` + hex, sin ceros a la izquierda)."""
    if value < 0:
        raise InvalidAmountError(
            f"no se puede codificar como cantidad JSON-RPC un valor negativo: {value}"
        )
    return hex(value)


def _return_word(value: object, index: int, *, field: str) -> int:
    """La palabra `index` de un retorno ABI, como entero sin signo.

    Un retorno de varias palabras no se puede leer con `_parse_quantity` entero
    —daría un número gigantesco formado por las palabras concatenadas—, así que
    se corta la palabra que interesa antes de convertirla.

    Un retorno más corto de lo esperado se rechaza en vez de rellenarse con
    ceros: significaría que la dirección o el selector no son los que se cree, y
    un cero silencioso ahí se leería como «no hay permiso», que es una decisión
    distinta de «no se pudo preguntar».

    Al cortar la palabra se le **devuelve el prefijo `0x`** antes de convertirla,
    y no es cosmética: sin él, `_parse_quantity` la leería como decimal —su otra
    forma aceptada— y `int("…0c573b")` lanzaría `ValueError` en cuanto la palabra
    tuviera una letra hexadecimal. Es decir, funcionaría por casualidad con los
    importes pequeños y fallaría con casi todos los reales, que es la peor forma
    de fallar. Se midió en la cadena: la primera venta de un ERC-20 murió aquí.
    """
    text = value.strip() if isinstance(value, str) else ""
    body = text[2:] if text[:2].lower() == "0x" else text
    start = index * 64
    if len(body) < start + 64:
        raise ExecutionError(
            f"el nodo devolvió un retorno de {len(body) // 2} bytes donde {field} "
            f"necesita al menos {(start + 64) // 2}: el contrato al que se preguntó "
            f"no es el que se cree."
        )
    return _parse_quantity("0x" + body[start : start + 64], field=field)


def _parse_quantity(value: object, *, field: str) -> int:
    """Cantidad JSON-RPC a entero.

    Acepta `"0x"` como cero porque **se midió**: hay nodos que devuelven `"0x"`
    para una cuenta de transacciones vacía, y `int("0x", 16)` sobre eso lanza
    `ValueError`. Un `ValueError` en el camino de firmar es un fallo
    incomprensible para quien lo lee, así que la rareza se absorbe aquí.
    """
    if isinstance(value, bool):
        # `bool` es subclase de `int` en Python: sin esto, `True` sería 1.
        raise ExecutionError(f"el nodo devolvió un booleano como {field}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text in {"", "0x", "0X"}:
            return 0
        try:
            return int(text, 16) if text[:2].lower() == "0x" else int(text)
        except ValueError as error:
            raise ExecutionError(
                f"el nodo devolvió un valor que no es una cantidad para {field}: "
                f"«{text[:40]}»"
            ) from error
    raise ExecutionError(
        f"el nodo devolvió {type(value).__name__} donde se esperaba una cantidad "
        f"para {field}"
    )


def word_address(address: str) -> str:
    """Una dirección EVM como palabra de 32 bytes, sin el `0x`.

    Se valida con `bytes.fromhex` en vez de contar caracteres: contar aceptaría
    `"zz…"` de 40 caracteres, y una dirección mal formada dentro de un `approve`
    autoriza a un contrato que no es el que se cree.
    """
    text = address.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    if len(text) != 40:
        raise InvalidAmountError(
            f"«{address}» no es una dirección EVM: se esperan 40 dígitos "
            f"hexadecimales y llegaron {len(text)}"
        )
    try:
        bytes.fromhex(text)
    except ValueError as error:
        raise InvalidAmountError(
            f"«{address}» tiene 40 caracteres pero no son hexadecimales"
        ) from error
    return text.lower().rjust(64, "0")


def word_uint(value: int) -> str:
    """Un entero sin signo como palabra de 32 bytes, sin el `0x`."""
    if value < 0:
        raise InvalidAmountError(f"un importe ERC-20 no puede ser negativo, llegó {value}")
    text = f"{value:x}"
    if len(text) > 64:
        raise InvalidAmountError(
            f"el importe {value} no cabe en una palabra de 32 bytes: es mayor que "
            f"2**256 - 1"
        )
    return text.rjust(64, "0")


def build_allowance_calldata(owner: str, spender: str) -> str:
    """`allowance(owner, spender)` codificado, para un `eth_call`."""
    return SELECTOR_ALLOWANCE + word_address(owner) + word_address(spender)


def build_approve_calldata(spender: str, amount_raw: int) -> str:
    """`approve(spender, amount)` codificado, para una transacción.

    El importe es **el exacto de esta operación**, nunca el máximo. Un
    `approve` ilimitado deja al contrato autorizado a vaciar ese token para
    siempre, y es el patrón que convierte un router comprometido en una pérdida
    total de esa posición. El precio de aprobar lo justo es una transacción de
    más la primera vez de cada par, y es el precio correcto.
    """
    return SELECTOR_APPROVE + word_address(spender) + word_uint(amount_raw)


def build_transfer_calldata(recipient: str, amount_raw: int) -> str:
    """`transfer(to, amount)` codificado: sacar un ERC-20 de la cartera.

    Es la llamada con la que se retira un token, y por eso **no** lleva el
    `from`: en ERC-20 el origen es siempre `msg.sender`, así que la única forma
    de mover el saldo de otro es `transferFrom` con un permiso previo. Eso es
    exactamente lo que aquí no se quiere —una retirada no debe necesitar que
    nadie haya aprobado nada— y por eso se emite desde la propia cartera.
    """
    return SELECTOR_TRANSFER + word_address(recipient) + word_uint(amount_raw)


def build_balance_of_calldata(owner: str) -> str:
    """`balanceOf(owner)` codificado, para un `eth_call` de sólo lectura."""
    return SELECTOR_BALANCE_OF + word_address(owner)


def build_is_approved_for_all_calldata(owner: str, operator: str) -> str:
    """`isApprovedForAll(owner, operator)` codificado, para un `eth_call`."""
    return SELECTOR_IS_APPROVED_FOR_ALL + word_address(owner) + word_address(operator)


def build_set_approval_for_all_calldata(operator: str, *, approved: bool) -> str:
    """`setApprovalForAll(operator, approved)` codificado, para una transacción.

    **No tiene variante con importe**, y eso no es un olvido de esta función: el
    estándar ERC-1155 no la tiene. Un ERC-20 se puede autorizar por la cantidad
    exacta que se va a mover —y así se hace aquí—, pero el permiso sobre un
    ERC-1155 es un booleano por colección y operador: o se tiene o no se tiene, y
    con él se pueden mover **todas** las participaciones de esa colección, no
    sólo las de esta operación.

    Por eso el camino que la usa lo dice en el diálogo con esas palabras. Un
    permiso sin límite presentado como si lo tuviera sería peor que el permiso:
    sería una decisión tomada sobre una descripción falsa.
    """
    bandera = word_uint(1 if approved else 0)
    return SELECTOR_SET_APPROVAL_FOR_ALL + word_address(operator) + bandera


def build_permit2_approve_calldata(
    token: str,
    spender: str,
    amount_raw: int,
    expiration: int,
) -> str:
    """`Permit2.approve(token, spender, amount, expiration)` codificado.

    Existe porque los routers modernos no mueven el token ellos mismos: lo piden
    a Permit2, y Permit2 sólo lo entrega si el dueño se lo ha autorizado **a
    él**, aparte del `approve` del ERC-20. Son dos permisos encadenados y
    ninguno sustituye al otro:

        ERC-20  ---approve-->  Permit2  ---approve-->  router

    El importe vuelve a ser el exacto de la operación, por la misma razón que en
    `build_approve_calldata`. La caducidad también se acota: un permiso sin
    caducidad sobrevive a la operación que lo motivó y sigue ahí meses después,
    que es exactamente el estado que nadie recuerda haber autorizado.
    """
    return (
        SELECTOR_PERMIT2_APPROVE
        + word_address(token)
        + word_address(spender)
        + word_uint(amount_raw)
        + word_uint(expiration)
    )


def build_permit2_allowance_calldata(owner: str, token: str, spender: str) -> str:
    """`Permit2.allowance(owner, token, spender)` codificado, para un `eth_call`.

    El orden de los argumentos es el de Permit2 y **no** el del ERC-20: aquí el
    token va en medio, porque un mismo dueño tiene un permiso por cada token y
    cada gastador. Leerlo con la firma del ERC-20 devolvería tres palabras donde
    se espera una, y la primera de ellas no sería el importe autorizado.
    """
    return (
        SELECTOR_PERMIT2_ALLOWANCE
        + word_address(owner)
        + word_address(token)
        + word_address(spender)
    )


@dataclass(frozen=True, slots=True)
class Permit2Allowance:
    """Lo que Permit2 tiene autorizado para un token y un gastador.

    Son dos datos y no uno porque el permiso de Permit2 tiene **importe y
    caducidad**: se concede por una cantidad y hasta una fecha, y caducado no
    vale nada aunque el importe sobre. Guardarlos juntos y contestar con una sola
    pregunta —`covers`— evita que quien consulte se acuerde de mirar la fecha; el
    olvido sería un swap firmado que revierte al cobrar.
    """

    amount_raw: int
    #: Segundo Unix hasta el que vale el permiso. `0` significa vencido: en
    #: Permit2 la caducidad es un `uint48` y cero es el pasado.
    expiration: int

    def covers(self, needed_raw: int, *, now: int) -> bool:
        """¿Alcanza el importe **y** sigue vigente?"""
        return self.amount_raw >= needed_raw and self.expiration >= now


@dataclass(frozen=True, slots=True)
class NetworkFees:
    """Lo que cuesta el gas ahora mismo, y de dónde salió cada cifra."""

    base_fee_per_gas: int
    observed_tip: int
    #: Cómo se obtuvo la propina: `maxPriorityFeePerGas`, `gasPrice` o el mínimo.
    tip_source: str
    max_fee_per_gas: int
    max_priority_fee_per_gas: int

    def describe(self) -> tuple[tuple[str, str], ...]:
        gwei = Decimal(10) ** 9
        return (
            ("Base fee", f"{Decimal(self.base_fee_per_gas) / gwei:.4f} gwei"),
            ("Propina observada", f"{Decimal(self.observed_tip) / gwei:.4f} gwei"),
            ("Origen de la propina", self.tip_source),
            ("Máximo por gas", f"{Decimal(self.max_fee_per_gas) / gwei:.4f} gwei"),
            (
                "Propina ofrecida",
                f"{Decimal(self.max_priority_fee_per_gas) / gwei:.4f} gwei",
            ),
        )


class EvmBroadcaster:
    """El estado de red y la emisión, para **una** red.

    Uno por red, y no uno que recorra el registro: el pool ya está indexado por
    red, y tener que pasar la red en cada llamada invita a pasar la equivocada en
    una de cada diez — y equivocarse aquí es firmar para una red y emitir en otra.
    """

    __slots__ = (
        "_chain_id",
        "_chain_key",
        "_clock",
        "_gas_policy",
        "_pool",
        "_receipt_poll_attempts",
        "_receipt_poll_interval_seconds",
    )

    def __init__(
        self,
        pool: RpcPool,
        chain_key: str,
        *,
        gas_policy: GasPolicy | None = None,
        clock: Clock | None = None,
        receipt_poll_attempts: int = 6,
        receipt_poll_interval_seconds: float = 2.0,
    ) -> None:
        self._pool = pool
        self._chain_key = chain_key
        self._gas_policy = gas_policy or GasPolicy()
        self._clock = clock or SystemClock()
        self._receipt_poll_attempts = max(receipt_poll_attempts, 0)
        self._receipt_poll_interval_seconds = max(receipt_poll_interval_seconds, 0.0)
        # Se resuelve en la primera llamada y se recuerda: el pool es de una sola
        # red, así que su chain id no cambia en la vida del proceso.
        self._chain_id: int | None = None

    @property
    def chain_key(self) -> str:
        return self._chain_key

    # ------------------------------------------------------------- estado  #
    async def chain_id(self) -> int:
        """El chain id que declara la red a la que se está hablando.

        Se pide de verdad en vez de darlo por sabido: una URL de RPC apuntando a
        otra red es un error de configuración de un carácter, y es exactamente el
        que convierte «swap en Base» en «swap en mainnet». La firma EIP-155 ya
        protege de emitir en la red equivocada; esto lo detecta **antes** de
        firmar, que es cuando todavía se puede explicar.
        """
        if self._chain_id is None:
            raw = await self._pool.call("eth_chainId")
            self._chain_id = _parse_quantity(raw, field="el chain id")
        return self._chain_id

    async def require_chain(self, expected_chain_id: int) -> None:
        """Comprueba que se habla con la red para la que se va a firmar."""
        actual = await self.chain_id()
        if actual != expected_chain_id:
            raise ExecutionError(
                f"el nodo configurado para «{self._chain_key}» dice ser la red "
                f"{actual}, pero la operación es para la red {expected_chain_id}. "
                f"No se firma nada: revisa la URL del endpoint."
            )

    async def pending_nonce(self, address: str) -> int:
        """Cuántas transacciones lleva enviadas esa cuenta, contando la mempool.

        `"pending"` y no `"latest"`: con `"latest"` se ignora lo que está sin
        minar, y dos operaciones seguidas salen con el mismo nonce — la segunda
        es rechazada, o peor, sustituye a la primera si sube la comisión.
        """
        raw = await self._pool.call("eth_getTransactionCount", [address, "pending"])
        return _parse_quantity(raw, field="el nonce pendiente")

    async def native_balance(self, address: str) -> int:
        """El saldo nativo en la unidad mínima (wei, gwei…), no en unidades humanas.

        Entero y no `Decimal`: es el mismo tipo que usa `UnsignedTransaction.value`,
        así que compararlos no pasa por coma flotante en ningún punto.
        """
        raw = await self._pool.call("eth_getBalance", [address, "latest"])
        return _parse_quantity(raw, field="el saldo nativo")

    async def has_code(self, address: str) -> bool:
        """Si en esa dirección hay un contrato desplegado.

        Se pregunta a la cadena con `eth_getCode` y no a un índice de terceros:
        un índice puede ir por detrás, y fiarse de él daría por desplegada una
        wallet que todavía no lo está — y una dirección sin código no puede
        pagar nada. `0x` es la respuesta para una cuenta sin código y también
        para una dirección en la que nadie ha escrito nunca; el `0x0` que
        devuelven algunos nodos significa lo mismo y se trata igual.
        """
        raw = await self._pool.call("eth_getCode", [address, "latest"])
        return str(raw).strip() not in ("0x", "0x0", "")

    async def token_balance(self, token: str, owner: str) -> int:
        """El saldo de un ERC-20, en su unidad mínima.

        Se lee del contrato del token y no de un índice: el índice puede ir por
        detrás, y un saldo por detrás haría rechazar una retirada que sí cabe —o
        aceptar una que no—. La cadena es la única fuente que no llega tarde.
        """
        raw = await self._pool.call(
            "eth_call",
            [{"to": token, "data": build_balance_of_calldata(owner)}, "latest"],
        )
        return _return_word(raw, 0, field="el saldo del token")

    async def read_fees(self) -> NetworkFees:
        """Base fee y propina de la red, y el techo que sale de la política.

        La propina se busca en tres sitios, de más a menos fiable, y se registra
        cuál funcionó: `eth_maxPriorityFeePerGas` no existe en todas las redes, y
        sin esta cascada una red que no lo implemente dejaría de poder operar.
        """
        block = await self._pool.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(block, dict):
            raise ExecutionError(
                f"el nodo de «{self._chain_key}» devolvió {type(block).__name__} "
                f"donde se esperaba el último bloque"
            )
        base_raw = block.get("baseFeePerGas")
        if base_raw is None:
            raise UnsupportedOperationError(
                f"la red «{self._chain_key}» no publica `baseFeePerGas`: no es una "
                f"red EIP-1559 y este camino de firma no la cubre. Firmarla exigiría "
                f"una transacción de tipo legado, que es otra implementación."
            )
        base_fee = _parse_quantity(base_raw, field="la base fee")

        tip, source = await self._observed_tip(base_fee)
        max_fee, max_tip = self._gas_policy.cap(base_fee, tip)
        return NetworkFees(
            base_fee_per_gas=base_fee,
            observed_tip=tip,
            tip_source=source,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=max_tip,
        )

    async def _observed_tip(self, base_fee: int) -> tuple[int, str]:
        try:
            raw = await self._pool.call("eth_maxPriorityFeePerGas")
            return _parse_quantity(raw, field="la propina sugerida"), "maxPriorityFeePerGas"
        except (RpcError, AllEndpointsFailedError) as error:
            _log.info(
                "evm.tip_unavailable",
                chain=self._chain_key,
                method="eth_maxPriorityFeePerGas",
                reason=str(error)[:120],
            )
        try:
            raw = await self._pool.call("eth_gasPrice")
            gas_price = _parse_quantity(raw, field="el precio del gas")
            # `eth_gasPrice` es un precio total, no una propina: lo que sobra por
            # encima de la base es lo que se está pagando por prioridad.
            return max(gas_price - base_fee, 0), "gasPrice"
        except (RpcError, AllEndpointsFailedError) as error:
            _log.warning(
                "evm.tip_fallback_to_floor",
                chain=self._chain_key,
                method="eth_gasPrice",
                reason=str(error)[:120],
                floor_wei=MIN_TIP_WEI,
            )
            return MIN_TIP_WEI, "mínimo de seguridad"

    async def read_allowance(self, token: str, owner: str, spender: str) -> int:
        """Cuánto puede gastar `spender` del `token` de `owner`, en unidades raw."""
        raw = await self._pool.call(
            "eth_call",
            [{"to": token, "data": build_allowance_calldata(owner, spender)}, "latest"],
        )
        return _parse_quantity(raw, field="el allowance")

    async def read_operator_approval(
        self, collection: str, owner: str, operator: str
    ) -> bool:
        """Si `operator` puede mover **toda** la colección ERC-1155 de `owner`.

        Es la pregunta equivalente a `read_allowance` para un token que no es
        ERC-20, y la diferencia no es de forma: no hay importe que leer. El
        estándar contesta sí o no, y ese «sí» alcanza a todas las participaciones
        de esa colección. Se comprueba de verdad, en vez de suponer que no está
        concedido, porque quien ya lo concedió no tiene por qué volver a pagar el
        gas de concederlo.
        """
        raw = await self._pool.call(
            "eth_call",
            [
                {
                    "to": collection,
                    "data": build_is_approved_for_all_calldata(owner, operator),
                },
                "latest",
            ],
        )
        return _parse_quantity(raw, field="el permiso de operador") != 0

    async def read_permit2_allowance(
        self, permit2: str, token: str, owner: str, spender: str
    ) -> Permit2Allowance:
        """El permiso que Permit2 tiene concedido, con su importe y su caducidad.

        Se leen las dos cosas porque el permiso de Permit2 **caduca**, al
        contrario que el del ERC-20: un permiso con importe de sobra pero vencido
        no sirve, y el swap revertiría al intentar cobrarlo. Comprobar sólo el
        importe daría por bueno un permiso que ya no lo es.
        """
        raw = await self._pool.call(
            "eth_call",
            [
                {
                    "to": permit2,
                    "data": build_permit2_allowance_calldata(owner, token, spender),
                },
                "latest",
            ],
        )
        return Permit2Allowance(
            amount_raw=_return_word(raw, 0, field="el importe autorizado en Permit2"),
            expiration=_return_word(raw, 1, field="la caducidad del permiso de Permit2"),
        )

    async def resolve_gas_limit(
        self, unsigned: UnsignedTransaction, *, from_address: str
    ) -> tuple[int, str]:
        """El límite de gas a firmar, y de dónde salió.

        Ver el docstring del módulo: un revert del nodo y un nodo que no
        responde son cosas distintas y se tratan distinto.
        """
        call: dict[str, Any] = {
            "from": from_address,
            "to": unsigned.to_address,
            "value": _quantity(unsigned.value.raw),
            "data": unsigned.calldata or "0x",
        }
        try:
            raw = await self._pool.call("eth_estimateGas", [call, "latest"])
        except RpcError as error:
            # El nodo contestó: la transacción revertiría tal cual está. Firmar
            # esto es pagar gas por una operación que no va a ocurrir.
            raise ExecutionError(
                f"la red «{self._chain_key}» estimó que «{unsigned.description}» "
                f"revertiría ({error.rpc_message}). No se firma: revisa el importe, "
                f"el allowance y los parámetros del swap."
            ) from error
        except AllEndpointsFailedError as error:
            if unsigned.gas_limit is None:
                raise ExecutionError(
                    f"no se pudo estimar el gas de «{unsigned.description}» y el "
                    f"payload no traía límite: sin ninguna de las dos cifras no hay "
                    f"nada que firmar. ({error})"
                ) from error
            _log.warning(
                "evm.gas_from_payload",
                chain=self._chain_key,
                gas_limit=unsigned.gas_limit,
                reason="ningún nodo respondió a eth_estimateGas",
            )
            return unsigned.gas_limit, "el payload del motor"

        estimated = _parse_quantity(raw, field="el gas estimado")
        if estimated <= 0:
            raise ExecutionError(
                f"el nodo estimó {estimated} gas para «{unsigned.description}»: esa "
                f"cifra no permite firmar."
            )
        con_margen = int(Decimal(estimated) * GAS_ESTIMATE_MARGIN)
        del_motor = unsigned.gas_limit or 0
        # El mayor de los dos: nunca por debajo de lo que la red pide, y nunca por
        # debajo de lo que el motor ya había calculado para este mismo payload.
        if del_motor >= con_margen:
            return del_motor, "el payload del motor (mayor que la estimación)"
        return con_margen, f"eth_estimateGas con margen {GAS_ESTIMATE_MARGIN}"

    # ------------------------------------------------------------ emitir  #
    async def send(
        self,
        signed: SignedEvmTransaction,
        *,
        expected_to: str | None = None,
    ) -> BroadcastReceipt:
        """Emite una transacción firmada y devuelve el recibo.

        `expected_to` es el destino que el motor declaró como medido. Es la
        última comprobación antes de algo irreversible y se hace aquí, en el
        único sitio por el que pasan todas las emisiones.
        """
        await self.require_chain(signed.chain_id)
        self._require_expected_destination(signed.to_address, expected_to)

        try:
            raw = await self._pool.call("eth_sendRawTransaction", [signed.raw_hex])
        except RpcError as error:
            if _is_already_accepted(error.rpc_message):
                # No es un fallo: el nodo ya la tiene. Se trata como aceptada y se
                # sigue al recibo como si la hubiéramos enviado nosotros.
                _log.info(
                    "evm.already_accepted",
                    chain=self._chain_key,
                    tx_hash=signed.tx_hash,
                    reason=error.rpc_message[:120],
                )
                return await self.wait_for_receipt(signed.tx_hash)
            raise BroadcastError(
                signed.tx_hash, self._chain_key, error.rpc_message
            ) from error
        except AllEndpointsFailedError as error:
            # Ambiguo de verdad: la petición pudo llegar a un nodo que la aceptó y
            # perderse la respuesta. Se devuelve `UNKNOWN` con el hash local —que
            # se calculó al firmar— para que se pueda comprobar en un explorador.
            # Marcarlo como fallo llevaría a firmar otra vez, y con otro nonce eso
            # sería una segunda operación idéntica.
            _log.warning(
                "evm.broadcast_unknown",
                chain=self._chain_key,
                tx_hash=signed.tx_hash,
                reason=str(error)[:200],
            )
            return BroadcastReceipt(
                tx_hash=signed.tx_hash,
                chain=self._chain_key,
                status=BroadcastStatus.UNKNOWN,
                observed_at=self._clock.now(),
                reason=(
                    "ningún nodo confirmó la recepción, así que la transacción puede "
                    "estar viva: comprueba ese hash antes de reintentar nada"
                ),
            )

        emitted = _parse_hash(raw, field="el hash devuelto al emitir")
        if emitted.lower() != signed.tx_hash.lower():
            # El hash lo calculamos nosotros al firmar. Si el nodo dice otro, o
            # firmamos algo distinto de lo que creemos, o el nodo responde por
            # otra cosa. Las dos son graves.
            raise ExecutionError(
                f"el nodo devolvió el hash {emitted}, pero el de la transacción "
                f"firmada es {signed.tx_hash}. No se puede asegurar qué se emitió."
            )

        _log.info(
            "evm.broadcast",
            chain=self._chain_key,
            tx_hash=signed.tx_hash,
            nonce=signed.nonce,
            gas_limit=signed.gas_limit,
            max_fee_per_gas=signed.max_fee_per_gas,
            max_cost_wei=signed.max_cost_wei,
        )
        return await self.wait_for_receipt(signed.tx_hash)

    async def wait_for_receipt(self, tx_hash: str) -> BroadcastReceipt:
        """Espera el recibo un rato acotado; si no llega, devuelve `PENDING`.

        No se espera a que mine. En una red congestionada eso son minutos, y la
        aplicación tiene que seguir usable; `PENDING` ya es prueba de que la
        transacción existe y es pública, que es lo que el registro necesita.
        """
        last_error = ""
        for _ in range(self._receipt_poll_attempts):
            try:
                raw = await self._pool.call("eth_getTransactionReceipt", [tx_hash])
            except (RpcError, AllEndpointsFailedError) as error:
                # Se sabe que el nodo la aceptó, así que el estado sigue siendo
                # `PENDING` y no `UNKNOWN`: lo que falta es el desenlace, no la
                # confirmación de que existe.
                last_error = str(error)[:200]
                break
            if raw is None:
                await asyncio.sleep(self._receipt_poll_interval_seconds)
                continue
            return self._receipt_from(raw, tx_hash)

        return BroadcastReceipt(
            tx_hash=tx_hash,
            chain=self._chain_key,
            status=BroadcastStatus.PENDING,
            observed_at=self._clock.now(),
            reason=last_error or "aún sin minar; el recibo puede consultarse después",
        )

    def _receipt_from(self, raw: object, tx_hash: str) -> BroadcastReceipt:
        if not isinstance(raw, dict):
            raise ExecutionError(
                f"el recibo de {tx_hash} no es un objeto JSON: llegó "
                f"{type(raw).__name__}"
            )
        block = raw.get("blockNumber")
        block_number = (
            None if block is None else _parse_quantity(block, field="el número de bloque")
        )
        status_raw = raw.get("status")
        observed = self._clock.now()

        if status_raw is None:
            # Redes anteriores a Byzantium no ponen `status`. Ninguna de las del
            # registro lo es, así que esto significa que el nodo contestó algo
            # incompleto — y decir «salió bien» sin saberlo sería justo lo que un
            # recibo no debe hacer.
            return BroadcastReceipt(
                tx_hash=tx_hash,
                chain=self._chain_key,
                status=BroadcastStatus.UNKNOWN,
                observed_at=observed,
                block_number=block_number,
                reason=(
                    "el recibo no trae `status`, así que no se sabe si la operación "
                    "salió bien; el bloque queda anotado para comprobarlo"
                ),
            )

        status_text = status_raw.lower() if isinstance(status_raw, str) else ""
        if status_text == _STATUS_SUCCESS:
            return BroadcastReceipt(
                tx_hash=tx_hash,
                chain=self._chain_key,
                status=BroadcastStatus.SUCCESS,
                observed_at=observed,
                block_number=block_number,
            )
        if status_text == _STATUS_REVERTED:
            return BroadcastReceipt(
                tx_hash=tx_hash,
                chain=self._chain_key,
                status=BroadcastStatus.REVERTED,
                observed_at=observed,
                block_number=block_number,
                reason=(
                    "la transacción se minó y revirtió: el gas se gastó y el swap no "
                    "ocurrió. El motivo concreto está en el explorador, en ese bloque."
                ),
            )
        raise ExecutionError(
            f"el recibo de {tx_hash} trae un `status` que no se entiende: "
            f"«{status_text[:20]}». Se esperaba {_STATUS_SUCCESS} o {_STATUS_REVERTED}."
        )

    def _require_expected_destination(self, actual: str, expected: str | None) -> None:
        if expected is None:
            _log.warning(
                "evm.destination_unchecked",
                chain=self._chain_key,
                to_address=actual,
                hint="el motor no declaró destino medido para esta red",
            )
            return
        if actual.lower() != expected.lower():
            raise ExecutionError(
                f"el destino de la transacción es {actual}, pero el motor declaró "
                f"{expected} como el contrato medido de «{self._chain_key}». No se "
                f"emite nada: un destino que no es el esperado puede ser un motor "
                f"comprometido o una respuesta manipulada."
            )


def _parse_hash(value: object, *, field: str) -> str:
    """Un hash de 32 bytes en hexadecimal."""
    if not isinstance(value, str):
        raise ExecutionError(
            f"el nodo devolvió {type(value).__name__} donde se esperaba {field}"
        )
    text = value.strip()
    if len(text) != 66 or text[:2].lower() != "0x":
        raise ExecutionError(
            f"el nodo devolvió algo que no es un hash de 32 bytes para {field}: "
            f"«{text[:40]}»"
        )
    try:
        bytes.fromhex(text[2:])
    except ValueError as error:
        raise ExecutionError(
            f"el hash devuelto para {field} no es hexadecimal: «{text[:40]}»"
        ) from error
    return text


def _is_already_accepted(message: str) -> bool:
    """Si el mensaje del nodo significa «ya tengo esa transacción»."""
    lowered = message.lower()
    return any(marca in lowered for marca in _ALREADY_ACCEPTED)


def broadcasters_by_chain(
    pools: dict[str, RpcPool],
    *,
    gas_policy: GasPolicy | None = None,
    clock: Clock | None = None,
    receipt_poll_attempts: int = 6,
    receipt_poll_interval_seconds: float = 2.0,
) -> dict[str, EvmBroadcaster]:
    """Un emisor por cada pool del registro.

    Se construyen todos al arrancar y no bajo demanda: un emisor es un objeto sin
    estado de red —el chain id se resuelve en la primera llamada y se recuerda—
    así que crearlos por adelantado no cuesta nada y evita que la primera
    ejecución real dependa de que alguien se acuerde de construirlo.
    """
    return {
        chain_key: EvmBroadcaster(
            pool,
            chain_key,
            gas_policy=gas_policy,
            clock=clock,
            receipt_poll_attempts=receipt_poll_attempts,
            receipt_poll_interval_seconds=receipt_poll_interval_seconds,
        )
        for chain_key, pool in pools.items()
    }


def require_evm(spec: ChainSpec) -> None:
    """Corta con un mensaje claro en las redes que este camino no cubre.

    Se pregunta por `spec.is_evm` y no por el nombre de la red: es el hecho del
    que depende de verdad que la firma EIP-1559 aplique, y una red nueva que no
    sea EVM queda cubierta sin tocar esta función.
    """
    if not spec.is_evm:
        raise UnsupportedOperationError(
            f"«{spec.name}» no es una red EVM: la aplicación sigue construyendo el "
            f"payload para que lo firmes fuera, pero todavía no firma ni emite en "
            f"esta red. Firmar en Solana exige `solders`, que es la entrega "
            f"siguiente."
        )
