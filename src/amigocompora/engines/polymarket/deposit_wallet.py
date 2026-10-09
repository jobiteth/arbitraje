"""Dirección de la deposit wallet de una cuenta de Polymarket. Sin red.

La deposit wallet es un proxy ERC-1967 con beacon desplegado por la factoría de
Polymarket con CREATE2. Su dirección se conoce antes de desplegarla: sale de la
clave de la EOA, la factoría y el beacon. Este módulo la calcula igual que el
cliente oficial `@polymarket/builder-relayer-client` 0.0.10 (`builder/derive.js`,
función `deriveBeaconDepositWallet`), y la prueba contra un vector que ese mismo
paquete produjo.

Si la dirección calculada no coincide con la que el relayer confirma en
`proxyAddress`, la cuenta no se puede operar: el relayer es la fuente de verdad
y este cálculo sólo sirve para saber de antemano a qué dirección enviar fondos.
"""

from __future__ import annotations

from typing import Any, Final

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from amigocompora.infra.evm.broadcast import word_address

#: Factoría de deposit wallets de Polymarket (documentación oficial).
FACTORY: Final = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

#: Beacon de las deposit wallets desplegadas después del 29 de junio de 2026.
#: Las anteriores son UUPS y no se derivan aquí.
BEACON: Final = "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"

#: Constantes de Solady `LibClone.initCodeHashERC1967BeaconProxy`, v0.1.26.
_BEACON_PREFIX: Final = 0x6100523D8160233D3973
_BEACON_CONST1: Final = "b3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3"
_BEACON_CONST2: Final = "1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c"
_BEACON_CONST3: Final = "60195155f3363d3d373d3d363d602036600436635c60da"


def _hex_bytes(text: str) -> bytes:
    return bytes.fromhex(text.removeprefix("0x"))


def _args(owner: str, factory: str) -> bytes:
    """`abi.encode(factory, bytes32(owner))`: los argumentos del proxy."""
    wallet_id = bytes(12) + _hex_bytes(owner)
    return _hex_bytes(word_address(factory)) + wallet_id


def _beacon_init_code_hash(beacon: str, args: bytes) -> bytes:
    """El hash del código de inicialización del proxy con beacon y argumentos."""
    prefijo = (_BEACON_PREFIX + (len(args) << 56)).to_bytes(10, "big")
    return keccak(
        prefijo
        + _hex_bytes(beacon)
        + _hex_bytes(_BEACON_CONST3)
        + _hex_bytes(_BEACON_CONST2)
        + _hex_bytes(_BEACON_CONST1)
        + args
    )


def derive_beacon_deposit_wallet(
    owner: str, *, factory: str = FACTORY, beacon: str = BEACON
) -> str:
    """La dirección de la deposit wallet de `owner` (la EOA que firma)."""
    args = _args(owner, factory)
    salt = keccak(args)
    init_code_hash = _beacon_init_code_hash(beacon, args)
    direccion = keccak(b"\xff" + _hex_bytes(factory) + salt + init_code_hash)[12:]
    return to_checksum_address(direccion)


#: Cadena del tipo `Order` tal como la hashea el recinto (V2). Su longitud, 186, va
#: dentro de la firma envuelta y el contrato la lee de ahí.
ORDER_TYPE_STRING: Final = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
_DOMAIN_TYPE_STRING: Final = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_ORDER_FIELDS: Final = (
    ("salt", "uint256"),
    ("maker", "address"),
    ("signer", "address"),
    ("tokenId", "uint256"),
    ("makerAmount", "uint256"),
    ("takerAmount", "uint256"),
    ("side", "uint8"),
    ("signatureType", "uint8"),
    ("timestamp", "uint256"),
    ("metadata", "bytes32"),
    ("builder", "bytes32"),
)
_NAME_EXCHANGE: Final = "Polymarket CTF Exchange"
_VERSION_EXCHANGE: Final = "2"
_NAME_WALLET: Final = "DepositWallet"
_VERSION_WALLET: Final = "1"
_ZERO_SALT: Final = bytes(32)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _word_address(address: str) -> bytes:
    return bytes(12) + _hex_bytes(address)


def _hash_order_fields(message: dict[str, Any]) -> bytes:
    """`keccak(ORDER_TYPE_HASH ‖ campos)`, cada campo en una palabra de 32 bytes."""
    palabras = [keccak(ORDER_TYPE_STRING.encode("utf-8"))]
    for name, kind in _ORDER_FIELDS:
        value = message[name]
        if kind == "address":
            palabras.append(_word_address(value))
        elif kind == "bytes32":
            palabras.append(_hex_bytes(value))
        else:
            palabras.append(_word(int(value)))
    return keccak(b"".join(palabras))


def _exchange_domain_separator(chain_id: int, exchange: str) -> bytes:
    return keccak(
        keccak(_DOMAIN_TYPE_STRING.encode("utf-8"))
        + keccak(_NAME_EXCHANGE.encode("utf-8"))
        + keccak(_VERSION_EXCHANGE.encode("utf-8"))
        + _word(chain_id)
        + _word_address(exchange)
    )


def _typed_data_sign(
    message: dict[str, Any], *, chain_id: int, exchange: str, wallet: str
) -> dict[str, Any]:
    order_types = [{"name": name, "type": kind} for name, kind in _ORDER_FIELDS]
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TypedDataSign": [
                {"name": "contents", "type": "Order"},
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
                {"name": "salt", "type": "bytes32"},
            ],
            "Order": order_types,
        },
        "primaryType": "TypedDataSign",
        "domain": {
            "name": _NAME_EXCHANGE,
            "version": _VERSION_EXCHANGE,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(exchange),
        },
        "message": {
            "contents": message,
            "name": _NAME_WALLET,
            "version": _VERSION_WALLET,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(wallet),
            "salt": "0x" + _ZERO_SALT.hex(),
        },
    }


def recover_wrapped_signer(
    message: dict[str, Any],
    signature: str,
    *,
    chain_id: int,
    exchange: str,
    wallet: str,
) -> str:
    """La EOA que firmó la parte interna de una firma envuelta. Sin red."""
    interna = bytes.fromhex(signature.removeprefix("0x"))[:65]
    typed = _typed_data_sign(message, chain_id=chain_id, exchange=exchange, wallet=wallet)
    return str(Account.recover_message(encode_typed_data(full_message=typed), signature=interna))


def wrap_order_signature(
    message: dict[str, Any],
    *,
    chain_id: int,
    exchange: str,
    wallet: str,
    private_key: str,
) -> str:
    """La firma ERC-7739 de una orden de deposit wallet, lista para el recinto.

    La clave de la EOA firma un `TypedDataSign` cuyo contenido es la orden, con el
    dominio de la deposit wallet dentro del mensaje y el del intercambio fuera. El
    resultado se concatena con los datos que la wallet necesita para reconstruir el
    hash en `isValidSignature`: la firma interna, el separador del intercambio, el
    hash del contenido, la cadena del tipo y su longitud en dos bytes.

    Reproduce byte a byte `buildOrderSignature` de `@polymarket/clob-client-v2`
    1.2.0 para `signatureType` 3; la prueba lo fija contra su salida.
    """
    typed = _typed_data_sign(message, chain_id=chain_id, exchange=exchange, wallet=wallet)
    firmada = Account.sign_message(encode_typed_data(full_message=typed), private_key)
    interna: bytes = firmada.signature
    cadena = ORDER_TYPE_STRING.encode("utf-8")
    partes = (
        interna
        + _exchange_domain_separator(chain_id, exchange)
        + _hash_order_fields(message)
        + cadena
        + len(cadena).to_bytes(2, "big")
    )
    return "0x" + partes.hex()
