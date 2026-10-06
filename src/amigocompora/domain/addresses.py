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
