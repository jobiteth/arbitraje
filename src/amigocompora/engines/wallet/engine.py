"""El motor de cartera: una dirección, todas las redes, un solo contrato.

### Qué es y qué no es

Lee. No firma, no emite y no acepta una clave privada — no hay ninguna función
en este paquete que las tome. Eso no es una omisión que se pueda rellenar luego
sin querer: un motor puede venir de un paquete de terceros y declara sus propios
hosts, así que uno que pudiera firmar sería un motor capaz de vaciar la cartera
que acaba de leer. La capacidad declarada es `READ_CHAIN`, y es la única.

### Dos familias, un contrato

EVM y Solana no comparten **nada** de cómo se lee un saldo, y por eso hay dos
lectores en módulos aparte en vez de un `if` en cada función:

- **Cómo se enumeran los tokens.** En EVM no se puede: hay que preguntar por
  contratos ya conocidos. En Solana la propia dirección enumera sus cuentas.
- **Cuántas llamadas cuesta una red.** En EVM, **una**: `Multicall3` trae el
  nativo y todos los tokens juntos. En Solana, tres: el nativo y una por cada
  programa de token.
- **Cuántos nodos públicos sirven.** En EVM, ocho redes con varios nodos cada
  una. En Solana, **uno**, y raciona por ventana.

La consecuencia práctica de lo último es que en Solana no se puede leer en
paralelo con confianza y en EVM sí, pero eso lo decide **quien llama** —el
reparto vive en el caso de uso— y no este motor.

### Por qué un motor y no dos

Podrían ser dos motores, uno por familia, y el registro tiene pilas justamente
para eso. No se hizo así a propósito: las pilas del registro son **alternativas
ordenadas** —«usa el mejor de estos»— y EVM y Solana no son alternativas, son
complementarias. En una pila, Solana sería el respaldo de EVM, y una cartera con
fondos en las dos redes enseñaría sólo una según cuál contestara antes. Aquí la
red elige al lector, y las dos se leen siempre.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import structlog

from amigocompora.domain.chains import CHAINS, AddressFormat
from amigocompora.domain.models import Token
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import EngineKind, EngineManifest, WalletEngine
from amigocompora.domain.wallet import ChainHoldings, WalletKind, WalletProfile
from amigocompora.engines.catalog import tokens_for
from amigocompora.engines.evm_rpc import ChainReader, rpc_hosts
from amigocompora.engines.wallet.evm import EvmReader
from amigocompora.engines.wallet.solana import SolanaReader

_log = structlog.get_logger(__name__)

#: Redes que este motor sabe leer. Se declara explícitamente y no se dice «todas»
#: porque lo que se cubre de verdad depende de que haya nodos **medidos**: una red
#: del registro sin endpoints no se puede leer, y prometerla daría una cartera con
#: una red que falla siempre y un usuario preguntándose por qué.
#:
#: Se deriva de la tabla de nodos en vez de escribirse a mano, por la misma razón
#: que `rpc_hosts`: una lista escrita aparte se separa de la realidad sin que nada
#: lo señale.
WALLET_CHAINS: Final[frozenset[str]] = frozenset(
    key for key in CHAINS if rpc_hosts((key,))
)

MANIFEST: Final = EngineManifest(
    engine_id="wallet",
    name="Cartera",
    version="1.0.0",
    kind=EngineKind.WALLET,
    summary=(
        "Lee los saldos de una dirección en todas las redes configuradas: la "
        "moneda nativa, las stablecoins y cualquier token, cada red por separado "
        "y todas juntas."
    ),
    # La única capacidad. No lleva PREPARE_TX ni SIGN_TX ni BROADCAST_TX, y no es
    # un olvido: este motor mira, y mirar no debe poder mover nada.
    capabilities=frozenset({Capability.READ_CHAIN}),
    wallet_chains=WALLET_CHAINS,
    allowed_hosts=rpc_hosts(WALLET_CHAINS),
)


class WalletEngineImpl:
    """El motor: elige lector por familia de red y delega.

    No hay lógica de saldos aquí. Lo que hay es la decisión de **quién lee**, que
    es la única pregunta que EVM y Solana no contestan igual.
    """

    __slots__ = ("_evm", "_reader", "_solana")

    def __init__(self, *, reader: ChainReader | None = None) -> None:
        # El lector se puede inyectar —y las pruebas lo hacen— porque es la
        # frontera de red del motor: todo lo demás es interpretación determinista
        # de una respuesta.
        self._reader = reader or ChainReader(WALLET_CHAINS, allowed_hosts=MANIFEST.allowed_hosts)
        self._evm = EvmReader(self._reader)
        self._solana = SolanaReader(self._reader)

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    async def aopen(self) -> None:
        await self._reader.aopen()

    async def aclose(self) -> None:
        await self._reader.aclose()

    async def holdings(self, profile: WalletProfile, chain_key: str) -> ChainHoldings:
        """Los saldos de una red, por el lector de su familia.

        El perfil decide el lector y la red decide **si esa cartera sirve para
        ella**. Las dos comprobaciones son necesarias y distintas: pasar una
        cartera de Solana con una red EVM no es un fallo del lector, es una
        pregunta que no tiene sentido, y contestarla con una lectura vacía haría
        parecer que la dirección no tiene fondos.
        """
        if chain_key not in CHAINS:
            return ChainHoldings(
                chain=chain_key, error=f"«{chain_key}» no es una red conocida"
            )
        if chain_key not in WALLET_CHAINS:
            return ChainHoldings(
                chain=chain_key,
                error=(
                    f"no hay nodos medidos para «{chain_key}», así que no se pueden "
                    f"leer sus saldos"
                ),
            )
        if not profile.covers(chain_key):
            return ChainHoldings(
                chain=chain_key,
                error=(
                    f"una cartera {profile.kind.label} no se puede leer en "
                    f"«{chain_key}»: son familias de dirección distintas"
                ),
            )

        if profile.kind is WalletKind.SOLANA:
            return await self._solana.holdings(chain_key, profile.address)
        return await self._evm.holdings(chain_key, profile.address, tokens_to_read(chain_key))


def tokens_to_read(chain_key: str) -> tuple[Token, ...]:
    """Los contratos por los que preguntar en una red EVM.

    En EVM esto no se puede deducir de la cadena —el saldo vive en el contrato
    del token y no hay lista de tenedores—, así que se pregunta por los que el
    proyecto tiene medidos en su catálogo. Un token que nadie nombró no aparece,
    y eso no es un fallo del motor: es cómo funciona EVM. La interfaz tiene que
    decir «pega el contrato para verlo» en vez de dar a entender que la lista
    está completa.

    Se incluye la moneda nativa con `address=None` porque el `aggregate3` la trae
    por `getEthBalance` dentro de la misma llamada, y así el nativo y los tokens
    viajan juntos y en orden — que es lo que permite emparejarlos por posición al
    interpretar la respuesta.
    """
    spec = CHAINS[chain_key]
    if spec.address_format is AddressFormat.SOLANA_BASE58:
        # En Solana los tokens se descubren, no se listan. El catálogo allí sólo
        # sirve para poner **nombre** a un mint, y el lector lo consulta por su
        # cuenta al construir cada holding.
        return ()
    nativo = Token(
        symbol=spec.native_symbol,
        decimals=spec.native_decimals,
        chain=chain_key,
        address=None,
    )
    return (nativo, *tokens_for(chain_key))


class WalletProvider:
    """Publica el motor en el entry point."""

    @property
    def manifest(self) -> EngineManifest:
        return MANIFEST

    def create(self, config: Mapping[str, str]) -> WalletEngineImpl:
        # Nada que configurar: ni clave, ni host, ni límite. El parámetro está
        # porque lo exige el contrato de `EngineProvider`, y no se lee nada de él
        # precisamente porque este motor no pide credenciales — que es lo que le
        # permite funcionar siempre.
        del config
        return WalletEngineImpl()


PROVIDER: Final = WalletProvider()

#: Comprobación en el import: si el motor dejara de satisfacer el protocolo, se
#: sabe al cargar el módulo y no la primera vez que alguien pide su cartera.
_: type[WalletEngine] = WalletEngineImpl
