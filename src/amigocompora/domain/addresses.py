"""Validación de direcciones.

Vive en el dominio porque es una invariante del negocio, no un detalle de
infraestructura: una dirección mal formada no debe llegar nunca a aparecer en
una transacción que el usuario va a confirmar.

Nota sobre el checksum EIP-55: validarlo requiere keccak256, que no está en la
librería estándar, y el dominio no admite dependencias externas. Aquí se valida
la **forma**; comprobar el checksum es trabajo del motor que ya depende de
`web3` (extra `[chain]`).
"""

from __future__ import annotations

import re
from typing import Final

from amigocompora.domain.errors import InvalidAmountError

_EVM_ADDRESS: Final = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: Dirección cero. Recibir fondos aquí los destruye, así que se rechaza
#: explícitamente como destinatario.
ZERO_ADDRESS: Final = "0x" + "0" * 40


def is_evm_address(value: str) -> bool:
    """Si `value` tiene la forma de una dirección EVM (0x + 40 hex)."""
    return bool(_EVM_ADDRESS.match(value))


def require_evm_address(value: str, field_name: str = "dirección") -> str:
    """Devuelve la dirección validada, o lanza `InvalidAmountError`."""
    if not is_evm_address(value):
        raise InvalidAmountError(
            f"{field_name} no válida: «{value}». Se espera 0x seguido de 40 dígitos hexadecimales."
        )
    if value.lower() == ZERO_ADDRESS:
        raise InvalidAmountError(
            f"{field_name} no puede ser la dirección cero: los fondos enviados ahí se pierden."
        )
    return value


def shorten(address: str, *, lead: int = 6, tail: int = 4) -> str:
    """`0x1234…abcd`, para tablas donde la dirección completa no cabe."""
    if len(address) <= lead + tail + 1:
        return address
    return f"{address[:lead]}…{address[-tail:]}"


# --------------------------------------------------------------------------- #
# Solana — direcciones en base58
# --------------------------------------------------------------------------- #
#: Alfabeto base58 de Bitcoin/Solana. Excluye `0`, `O`, `I` y `l` justamente
#: para que una dirección dictada en voz alta no sea ambigua.
_B58_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX: Final = {char: index for index, char in enumerate(_B58_ALPHABET)}

#: Un pubkey de Solana son 32 bytes. La codificación ocupa entre 32 y 44
#: caracteres según cuántos ceros a la izquierda lleve.
SOLANA_PUBKEY_BYTES: Final = 32

#: El pubkey todo a ceros: es la dirección del **programa** System, no una
#: cartera. Enviarle fondos los pierde igual que en EVM, así que se rechaza como
#: destinatario con el mismo criterio que `ZERO_ADDRESS`.
SOLANA_SYSTEM_PROGRAM: Final = "1" * 32


def _b58_decode(text: str) -> bytes | None:
    """Decodifica base58 a bytes, o `None` si hay un carácter fuera del alfabeto.

    Se decodifica de verdad —y no se comprueba con una expresión regular— porque
    el alfabeto por sí solo no garantiza nada: `"z" * 44` son 44 caracteres
    válidos y no son un pubkey de 32 bytes. La única forma de saberlo es
    convertir y medir.
    """
    number = 0
    for char in text:
        digit = _B58_INDEX.get(char)
        if digit is None:
            return None
        number = number * 58 + digit
    # Cada `1` inicial es un byte cero que la conversión a entero pierde.
    leading_zeros = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_zeros + body


def is_solana_address(value: str) -> bool:
    """Si `value` decodifica a un pubkey de Solana de 32 bytes."""
    if not 32 <= len(value) <= 44:
        return False
    decoded = _b58_decode(value)
    return decoded is not None and len(decoded) == SOLANA_PUBKEY_BYTES


def require_solana_address(value: str, field_name: str = "dirección") -> str:
    """Devuelve la dirección validada, o lanza `InvalidAmountError`."""
    if not is_solana_address(value):
        raise InvalidAmountError(
            f"{field_name} no válida: «{value}». Se espera un pubkey de Solana en "
            f"base58 que decodifique a {SOLANA_PUBKEY_BYTES} bytes."
        )
    if value == SOLANA_SYSTEM_PROGRAM:
        raise InvalidAmountError(
            f"{field_name} no puede ser el programa System: los fondos enviados "
            f"ahí se pierden."
        )
    return value
