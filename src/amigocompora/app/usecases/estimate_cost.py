"""Cuánto costará en la moneda nativa la transacción que se acaba de preparar.

### Qué es, y qué no

Es una **estimación de coste máximo**, no un cobro: el producto del límite de
gas por la tarifa máxima (`gas_limit * max_fee_per_gas`), la misma semántica que
`SignedTransaction.max_cost_wei`. El coste real puede ser
menor —el gas que sobre del límite no se paga— y puede moverse antes de emitir,
porque la tarifa base de la red cambia cada bloque. Se enseña con «≈» y su
tooltip lo dice.

### De dónde salen las dos cifras

Ninguna se inventa aquí: las dos salen del emisor de la red, que es el mismo
objeto que se usa al firmar.

- El límite lo resuelve `EvmBroadcaster.resolve_gas_limit`, que pregunta
  `eth_estimateGas` **desde la cartera que va a firmar** (el gas depende de
  quién envía: sin allowance, la transacción revierte) y devuelve también de
  dónde salió la cifra final.
- La tarifa máxima la lee `read_fees()` de la red.

### Por qué existe como caso de uso aparte

La estimación es una llamada de red por ruta, así que **no** se hace al cotizar
—cotizar enseña muchas rutas y costaría una estimación por cada una, y ninguna
se va a firmar hasta que el usuario elija—. Se hace sólo cuando se prepara, que
es el momento en que el usuario ya eligió, y con el payload ya construido.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from amigocompora.domain.errors import ExecutionError
from amigocompora.domain.models import UnsignedTransaction
from amigocompora.domain.money import TokenAmount
from amigocompora.engines.catalog import native_token
from amigocompora.infra.evm.broadcast import EvmBroadcaster


@dataclass(frozen=True, slots=True)
class NetworkCost:
    """El coste máximo estimado, con las cifras que lo sostienen.

    `native` es el límite por la tarifa máxima, en la moneda de la red (ETH,
    POL, BNB…). `source` dice de dónde salió el límite —medido por la red o
    declarado por el motor—, porque un número medido y uno declarado no merecen
    la misma confianza y la interfaz no puede hacerlos indistinguibles.
    """

    native: TokenAmount
    gas_limit: int
    gas_price_wei: int
    source: str

    @property
    def gas_price_gwei(self) -> Decimal:
        """La tarifa máxima por gas, en gwei: la unidad en la que se lee."""
        return Decimal(self.gas_price_wei) / (Decimal(10) ** 9)


@dataclass(frozen=True, slots=True)
class EstimateNetworkCost:
    """Estima el coste de red de un payload EVM contra el emisor de su red."""

    #: Los **mismos** emisores que firman: un mapa propio sería un segundo sitio
    #: donde el pool de una red puede quedar distinto del otro sin que nada lo
    #: diga, y aquí se está enseñando una cifra que el usuario va a comparar
    #: contra el gas que de verdad pague.
    broadcasters: Mapping[str, EvmBroadcaster]

    async def __call__(
        self,
        unsigned: UnsignedTransaction,
        chain_key: str,
        *,
        from_address: str,
    ) -> NetworkCost:
        """El coste máximo estimado de `unsigned` en la red `chain_key`.

        `from_address` es la cartera que firmaría: el gas depende de quién
        envía —un swap sin allowance revierte y el nodo lo dice—, así que
        estimar desde otra dirección daría una cifra de otra transacción.

        Lanza `ExecutionError` si no hay nodo para esa red o si la red estimó
        que la transacción revertiría. Quien llame decide: preparar un borrador
        no debe caerse porque la estimación no se pudo hacer.
        """
        broadcaster = self.broadcasters.get(chain_key)
        if broadcaster is None:
            raise ExecutionError(
                f"no hay ningún nodo configurado para «{chain_key}»: sin él no se "
                f"puede estimar el coste de red de la transacción. Declara al "
                f"menos un endpoint para esa red."
            )
        gas_limit, source = await broadcaster.resolve_gas_limit(
            unsigned, from_address=from_address
        )
        fees = await broadcaster.read_fees()
        # El símbolo y los decimales salen del registro de redes, no del payload:
        # es la fuente que ya usa todo lo demás para nombrar la moneda nativa.
        nativo = native_token(chain_key)
        return NetworkCost(
            native=TokenAmount(
                raw=gas_limit * fees.max_fee_per_gas,
                decimals=nativo.decimals,
                symbol=nativo.symbol,
            ),
            gas_limit=gas_limit,
            gas_price_wei=fees.max_fee_per_gas,
            source=source,
        )
