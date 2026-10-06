"""Registro de redes: qué cadenas conoce la aplicación y cómo se identifican.

Vive en el dominio porque la identidad de una red es un hecho del negocio, no
un detalle de ninguna fuente: la UI necesita el nombre para el selector, los
motores necesitan la clave para traducirla a su propio slug, y el dominio
necesita saber qué formato de dirección es válido en cada una.

**Añadir una red es una entrada en `CHAINS` y un slug en cada motor que la
cubra.** Nada más: ni condicionales nuevos, ni subclases. Eso es lo que hace
que esto escale.

---

### Por qué la clave de red es un `str` y no un chain id numérico

El chain id EIP-155 es un invento de Ethereum. Solana no tiene ninguno, y
asignarle uno inventado sería meter una mentira en el tipo que identifica las
redes: cualquier código que luego lo usara para firmar o para hablar JSON-RPC
estaría trabajando con un número que no existe.

Así que la identidad de una red en el dominio es su clave estable (`"ethereum"`,
`"solana"`), y el chain id EIP-155 es un **atributo opcional** de las redes que
lo tienen. Quien necesita el número —el pool de RPC, una transacción sin
firmar— lo pide explícitamente y se encuentra con `None` si la red no es EVM,
que es exactamente la conversación que debe tener.

### Los datos de abajo están medidos, no recordados

Cada `eip155_id` sale del cruce de la lista de redes de GeckoTerminal con el
campo `chain_identifier` de las plataformas de CoinGecko. Cada `native_decimals`
y cada dirección del catálogo salen de la propia fuente. Un decimal equivocado
no produce un error: produce un precio mil veces mayor o menor, y una
comparación que parece correcta.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from amigocompora.domain.errors import InvalidAmountError, UnknownChainError


class AddressFormat(StrEnum):
    """Formato de dirección de una red. Decide cómo se valida y se compara.

    No es cosmético: en EVM las direcciones se comparan en minúsculas porque las
    fuentes discrepan en el *checksum* EIP-55, mientras que en Solana la
    codificación base58 **distingue mayúsculas** y pasarla a minúsculas la
    destruye. Una sola regla de comparación para las dos sería incorrecta en
    una de ellas.
    """

    EVM_HEX = "evm_hex"
    SOLANA_BASE58 = "solana_base58"


_EVM_ADDRESS: Final = re.compile(r"^0x[0-9a-fA-F]{40}$")
#: base58 de Bitcoin: alfabeto sin `0`, `O`, `I` ni `l`, justo para que no se
#: confundan al leerlas. Las cuentas de Solana son de 32 bytes, 32-44 caracteres.
_BASE58_ADDRESS: Final = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True, slots=True)
class ChainSpec:
    """Una red que la aplicación sabe consultar."""

    key: str
    name: str
    native_symbol: str
    native_decimals: int
    address_format: AddressFormat
    #: Chain id EIP-155, o `None` si la red no es EVM.
    eip155_id: int | None = None
    #: Dirección del token nativo envuelto (WETH, WBNB…), cuando existe. Es el
    #: token con el que se cotiza de verdad: los AMM no operan con el nativo.
    wrapped_native: str | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise InvalidAmountError("la red necesita una clave")
        if self.native_decimals < 0:
            raise InvalidAmountError(
                f"native_decimals de «{self.key}» debe ser >= 0, llegó {self.native_decimals}"
            )
        if self.eip155_id is not None and self.eip155_id <= 0:
            raise InvalidAmountError(
                f"el chain id EIP-155 de «{self.key}» debe ser positivo, llegó {self.eip155_id}"
            )

    @property
    def is_evm(self) -> bool:
        return self.address_format is AddressFormat.EVM_HEX

    @property
    def native_sentinel(self) -> str | None:
        """Dirección con la que esta red identifica a su nativo sin envolver.

        Sólo existe en EVM, y es `NATIVE_SENTINEL`: un invento de Uniswap V4 para
        poder operar el nativo sin pasar por el ERC-20. En Solana no hay
        equivalente —SOL envuelto es un mint SPL normal y corriente, con su
        dirección de verdad—, así que aquí devuelve `None`.

        Que esto sea una propiedad de la red y no una constante que cada motor
        usa a su aire tiene una razón medida: preguntarle a la API de Solana por
        los pools de `0x0000…0000` devuelve 404, porque esa dirección no
        significa nada ahí. La red es quien sabe si tiene centinela.
        """
        return NATIVE_SENTINEL if self.is_evm else None

    def require_eip155_id(self) -> int:
        """Chain id EIP-155, o un error que explica por qué no lo hay.

        Lo llama quien de verdad necesita el número —JSON-RPC, EIP-155— para
        que el fallo sea una frase legible y no un `None` propagándose.
        """
        if self.eip155_id is None:
            raise UnknownChainError(
                f"«{self.name}» no es una red EVM y no tiene chain id EIP-155: "
                f"la operación que lo pide no se puede hacer en esta red."
            )
        return self.eip155_id

    def normalize_address(self, address: str) -> str:
        """Forma canónica de una dirección de esta red, para compararlas.

        En EVM se pasa a minúsculas; en Solana se deja intacta, porque base58
        distingue mayúsculas.
        """
        text = address.strip()
        return text.lower() if self.address_format is AddressFormat.EVM_HEX else text

    def is_valid_address(self, address: str) -> bool:
        pattern = _EVM_ADDRESS if self.address_format is AddressFormat.EVM_HEX else _BASE58_ADDRESS
        return bool(pattern.match(address.strip()))

    def require_address(self, address: str) -> str:
        if not self.is_valid_address(address):
            raise InvalidAmountError(
                f"«{address}» no es una dirección válida en {self.name} "
                f"(formato esperado: {self.address_format.value})"
            )
        return self.normalize_address(address)


#: Dirección cero: en Uniswap V4 identifica al **token nativo**, que ahora se
#: puede operar sin envolverlo. Aparece de verdad en los pools medidos, así que
#: hay que reconocerla en vez de tratarla como una dirección cualquiera.
#:
#: Es un concepto **de EVM**. Quien la necesite debe pedirla por
#: `ChainSpec.native_sentinel`, que devuelve `None` en las redes donde no
#: significa nada, en vez de usar esta constante a pelo.
NATIVE_SENTINEL: Final = "0x0000000000000000000000000000000000000000"


def _evm(
    key: str,
    name: str,
    eip155_id: int,
    *,
    native_symbol: str = "ETH",
    native_decimals: int = 18,
    wrapped_native: str | None = None,
) -> ChainSpec:
    return ChainSpec(
        key=key,
        name=name,
        native_symbol=native_symbol,
        native_decimals=native_decimals,
        address_format=AddressFormat.EVM_HEX,
        eip155_id=eip155_id,
        wrapped_native=wrapped_native,
    )


#: Las redes que la aplicación conoce, por clave.
#:
#: Los `eip155_id` se obtuvieron cruzando la lista de redes de GeckoTerminal con
#: el `chain_identifier` de CoinGecko. Las direcciones envueltas se midieron en
#: los pools de más volumen de cada red: el token nativo envuelto aparece en
#: casi todos, así que sale por frecuencia y no por suposición.
CHAINS: Final[Mapping[str, ChainSpec]] = {
    chain.key: chain
    for chain in (
        _evm(
            "ethereum",
            "Ethereum",
            1,
            wrapped_native="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        ),
        _evm(
            "base",
            "Base",
            8453,
            wrapped_native="0x4200000000000000000000000000000000000006",
        ),
        _evm(
            "bsc",
            "BNB Chain",
            56,
            native_symbol="BNB",
            wrapped_native="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        ),
        _evm(
            "arbitrum",
            "Arbitrum One",
            42161,
            wrapped_native="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        ),
        _evm(
            "polygon",
            "Polygon PoS",
            137,
            native_symbol="POL",
            wrapped_native="0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        ),
        _evm(
            "unichain",
            "Unichain",
            130,
            wrapped_native="0x4200000000000000000000000000000000000006",
        ),
        _evm(
            "optimism",
            "OP Mainnet",
            10,
            wrapped_native="0x4200000000000000000000000000000000000006",
        ),
        _evm(
            "avalanche",
            "Avalanche C-Chain",
            43114,
            native_symbol="AVAX",
            wrapped_native="0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",
        ),
        # Arc cobra el gas en USDC, no en un token nativo propio: por eso su
        # `native_coin_id` en CoinGecko es `usd-coin`, y por eso no tiene
        # envoltorio nativo que cotizar. Medido: USDC aparece en los 20 pools
        # de más volumen de la red, en los 20.
        _evm("arc", "Arc", 5042, native_symbol="USDC", native_decimals=6),
        # Robinhood Chain envuelve ETH en su propia dirección, no en la
        # `0x4200…0006` de las OP Stack. Medido en sus pools principales.
        _evm(
            "robinhood",
            "Robinhood Chain",
            4663,
            wrapped_native="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        ),
        ChainSpec(
            key="solana",
            name="Solana",
            native_symbol="SOL",
            native_decimals=9,
            address_format=AddressFormat.SOLANA_BASE58,
            eip155_id=None,
            wrapped_native="So11111111111111111111111111111111111111112",
        ),
    )
}

#: Red que se muestra al abrir la aplicación.
DEFAULT_CHAIN: Final = "ethereum"


def chain(key: str) -> ChainSpec:
    """Devuelve la red de clave `key`, o un error con las que sí existen."""
    found = CHAINS.get(key)
    if found is None:
        raise UnknownChainError(
            f"la red «{key}» no está en el registro. Disponibles: {', '.join(sorted(CHAINS))}."
        )
    return found


def chain_by_eip155_id(eip155_id: int) -> ChainSpec | None:
    """Busca una red por su chain id EIP-155. `None` si no está registrada."""
    for spec in CHAINS.values():
        if spec.eip155_id == eip155_id:
            return spec
    return None


def evm_chains() -> tuple[ChainSpec, ...]:
    """Redes EVM, las únicas donde tiene sentido hablar JSON-RPC."""
    return tuple(spec for spec in CHAINS.values() if spec.is_evm)
