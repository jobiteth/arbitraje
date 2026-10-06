"""Firma EIP-1559. Criptografía pura: sin red, sin estado, sin secretos guardados.

Aquí no se escribe RLP ni secp256k1 a mano — `eth-account` lo hace, y hacerlo a
mano sería la forma más rápida de perder dinero por un bug sutil. Lo que sí es
responsabilidad de este módulo es **todo lo demás**: validar la clave sin
repetirla, verificar que lo firmado es lo que se cree, y no dejar ni rastro de
la clave en el resultado.

### La clave no se repite, ni siquiera cuando está mal

La regla de que una clave privada no aparece en un log ni en un `repr()` es
fácil de cumplir en el camino feliz y facilísima de romper en el camino de
error: basta con propagar el mensaje de una librería, o con escribir un
`raise ValueError(f"clave inválida: {key}")`. Este módulo no lo hace. Cuando la
clave no vale, el mensaje describe **la forma** del problema —cuántos dígitos
llegaron, qué carácter sobra— y nunca el valor. Es un detalle, y es la clase de
detalle que decide si un día la clave de alguien acaba en un fichero de log
porque pegó mal el texto.

### Por qué se recupera la firma después de firmar

`sign_eip1559` vuelve a derivar la dirección desde los bytes firmados antes de
devolverlos. Cuesta una recuperación secp256k1 —del orden de un milisegundo— y
a cambio convierte «confío en que la librería firmó con esta clave» en un hecho
comprobado, justo antes de emitir algo irreversible. Una firma que se pudiera
emitir con una clave que no es la que el usuario cree es exactamente el fallo
que no se puede detectar después.
"""

from __future__ import annotations

from typing import Any, Final, cast

import structlog
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

from amigocompora.domain.addresses import require_evm_address
from amigocompora.domain.errors import (
    ExecutionError,
    InvalidAmountError,
    KeyCustodyError,
)
from amigocompora.domain.models import SignedEvmTransaction, UnsignedTransaction

_log = structlog.get_logger(__name__)

#: Una clave privada secp256k1 son 32 bytes, es decir 64 dígitos hexadecimales.
_KEY_HEX_LENGTH: Final = 64

#: Como se serializa una llamada vacía. `eth-account` acepta `b""`, pero el
#: `0x` es lo que el usuario ve en el payload y lo que espera un explorador.
_EMPTY_CALLDATA: Final = "0x"


def _account_from_key(private_key: str) -> LocalAccount:
    """Valida la clave y devuelve la cuenta. Único punto donde se toca la clave.

    Todo lo que pueda citar la clave en un mensaje de error se corta aquí: los
    mensajes de `eth-account` describen el problema, pero un `ValueError` de
    `int(..., 16)` sobre una cadena no hexadecimal **sí** incluiría el texto
    recibido, y una clave privada mal pegada no debe terminar en el log por eso.
    """
    candidate = private_key.strip()
    if candidate[:2].lower() == "0x":
        candidate = candidate[2:]

    if len(candidate) != _KEY_HEX_LENGTH:
        raise KeyCustodyError(
            f"la clave privada no tiene la forma de una clave secp256k1: se esperan "
            f"{_KEY_HEX_LENGTH} dígitos hexadecimales (32 bytes) y llegaron "
            f"{len(candidate)}. No se muestra el valor: una clave privada no se "
            f"repite, ni aquí ni en un log."
        )
    try:
        raw = bytes.fromhex(candidate)
    except ValueError as error:
        raise KeyCustodyError(
            "la clave privada contiene caracteres que no son hexadecimales. "
            "El valor no se reproduce aquí por la misma razón por la que no se "
            "registra en ningún sitio."
        ) from error

    try:
        # `Account.from_key` no está anotado en `eth-account`, así que devuelve
        # `Any`; el `cast` es lo que hace que el resto del módulo se tipifique de
        # verdad en vez de propagar el `Any` hacia fuera.
        return cast("LocalAccount", Account.from_key(raw))
    except (ValueError, TypeError) as error:
        # `eth-account` rechaza escalares fuera del rango de la curva. El tipo
        # de excepción basta para saber qué pasó; el mensaje original se
        # descarta porque puede citar la clave.
        raise KeyCustodyError(
            f"la clave privada tiene el formato correcto pero no es un escalar "
            f"válido de secp256k1 ({type(error).__name__}). Puede estar fuera de "
            f"rango o no ser una clave real."
        ) from error


def address_from_key(private_key: str) -> str:
    """La dirección pública de esa clave, con checksum EIP-55.

    Derivarla es pública y barata, así que la UI puede mostrar de qué cartera
    saldría el dinero **sin** tener la clave cargada más tiempo del necesario.
    """
    return _account_from_key(private_key).address


def _require_signable(
    *,
    nonce: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> None:
    """Comprueba el estado de red antes de dárselo a la librería.

    Se valida aquí en vez de confiar en que la librería rechace lo inválido
    porque lo que hace al rechazarlo es **serializar RLP**, y un nonce negativo
    no falla ahí con un error del dominio: falla con un
    `rlp.exceptions.ObjectSerializationError` que habla de campos y sedes. Un
    dato malo puesto por quien llama tiene que producir un mensaje que nombre el
    campo en el vocabulario del producto, y además así no se gasta ni una
    operación de curva antes de saber que no se puede firmar.

    Estas mismas invariantes las vuelve a exigir `SignedEvmTransaction`. La
    repetición es deliberada: aquí se comprueba **antes** de firmar y allí se
    garantiza que ningún objeto construido por otra vía pueda saltárselas.
    """
    if nonce < 0:
        raise InvalidAmountError(
            f"el nonce no puede ser negativo, llegó {nonce}: no existe una cuenta "
            f"con esa cuenta de transacciones enviadas."
        )
    if gas_limit <= 0:
        raise InvalidAmountError(
            f"el límite de gas debe ser positivo, llegó {gas_limit}. Sin él la "
            f"transacción no se puede firmar."
        )
    if max_fee_per_gas <= 0:
        raise InvalidAmountError(
            f"el precio máximo por gas debe ser positivo, llegó {max_fee_per_gas}"
        )
    if max_fee_per_gas < max_priority_fee_per_gas:
        raise InvalidAmountError(
            f"el precio máximo por gas ({max_fee_per_gas}) es menor que la propina "
            f"({max_priority_fee_per_gas}): la transacción nunca sería aceptable y "
            f"el nodo la rechazaría."
        )


def sign_eip1559(
    unsigned: UnsignedTransaction,
    *,
    private_key: str,
    nonce: int,
    gas_limit: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> SignedEvmTransaction:
    """Firma `unsigned` y devuelve la transacción lista para emitir.

    Recibe el nonce y las comisiones ya resueltos, en vez de consultarlos: son
    estado de la red y consultarlos aquí ataría esta función a un nodo,
    convirtiendo una operación local y reproducible en una que depende de la
    red. La firma es lo único irreversible que pasa en este fichero, y se
    mantiene determinista.

    `to_address` viaja a la librería **con checksum**, aunque el motor lo haya
    dado en minúsculas, y se conserva tal cual —sin recomponer— en el
    `SignedEvmTransaction` que se devuelve. No es cosmética: `eth-account`
    rechaza una dirección sin checksum en cuanto contiene alguna letra, con un
    `TypeError: Transaction had invalid fields`, así que sin esta conversión **no
    se firmaba ningún swap real** — la tabla de routers de los motores está
    escrita en minúsculas. Se midió, y la prueba del firmador no lo veía porque
    su router de ejemplo es todo dígitos, que es justo el único caso que pasa.

    Lo que se conserva es la forma que el motor dio, para que el destino que el
    usuario ve en pantalla sea el mismo que el motor declara y contra el que se
    contrasta: la conversión es un detalle del formato que exige la librería, no
    un cambio del dato.
    """
    _require_signable(
        nonce=nonce,
        gas_limit=gas_limit,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    account = _account_from_key(private_key)

    # El destino pasa por la validación del dominio —que incluye rechazar la
    # dirección cero, a la que lo que llegue se pierde— y después por la forma
    # que exige la librería. Los dos pasos, y en ese orden: validar primero da un
    # mensaje que nombra el campo, y convertir después es lo que hace que la firma
    # no falle por una mayúscula.
    destino = to_checksum_address(
        require_evm_address(unsigned.to_address, "destino del swap")
    )

    # `Any` explícito y no `object` porque la anotación de `eth-account` es una
    # unión de cinco tipos y satisfacerla exigiría mentir sobre cada valor: son
    # un entero, dos cadenas y dos enteros grandes. El `Any` está acotado a este
    # diccionario, que se construye aquí mismo y no sale del módulo.
    signable: dict[str, Any] = {
        "type": 2,
        "chainId": unsigned.chain_id,
        "nonce": nonce,
        "to": destino,
        "value": unsigned.value.raw,
        "data": unsigned.calldata or _EMPTY_CALLDATA,
        "gas": gas_limit,
        "maxFeePerGas": max_fee_per_gas,
        "maxPriorityFeePerGas": max_priority_fee_per_gas,
    }

    try:
        signed = account.sign_transaction(signable)
    except Exception as error:
        # A propósito ciego. La jerarquía de `eth-account` y `rlp` es ancha,
        # cambia entre versiones, y sus excepciones no heredan de nada que se
        # pueda nombrar aquí: el `SerializationError` de `rlp` es un `Exception`
        # pelado, y un nonce negativo llegaba a la UI como un volcado de sedes
        # RLP. Los datos mal puestos ya se filtraron antes, en
        # `_require_signable`; lo que queda aquí es la librería fallando.
        #
        # No filtra nada: se reproduce el mensaje de la librería, que describe
        # la transacción y nunca la clave — la clave no entra en la
        # serialización, que es justamente lo que hace una firma.
        raise ExecutionError(
            f"no se pudo firmar la transacción para «{unsigned.description}»: "
            f"la librería de firma falló con {type(error).__name__}: {error}"
        ) from error

    raw_hex = signed.raw_transaction.to_0x_hex()
    tx_hash = signed.hash.to_0x_hex()

    # La comprobación que convierte una suposición en un hecho: si los bytes que
    # se van a emitir no se recuperan como la dirección que se va a mostrar, es
    # que no se firmó con la clave que se cree, y eso hay que saberlo antes de
    # emitir y no después.
    recovered = Account.recover_transaction(raw_hex)
    if recovered.lower() != account.address.lower():
        raise ExecutionError(
            f"la firma no se recupera como la cartera que firma: se esperaba "
            f"{account.address} y se obtuvo {recovered}. No se emite nada."
        )

    _log.info(
        "evm.signed",
        tx_hash=tx_hash,
        from_address=account.address,
        chain_id=unsigned.chain_id,
        nonce=nonce,
        gas_limit=gas_limit,
    )

    return SignedEvmTransaction(
        raw_hex=raw_hex,
        tx_hash=tx_hash,
        from_address=account.address,
        chain_id=unsigned.chain_id,
        nonce=nonce,
        to_address=unsigned.to_address,
        value=unsigned.value,
        gas_limit=gas_limit,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        calldata=unsigned.calldata,
        description=unsigned.description,
    )
