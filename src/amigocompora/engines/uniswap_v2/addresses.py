"""Dónde está cada DEX de estilo Uniswap V2, **medido red por red**.

## Qué se midió, y cómo

Cada entrada pasó cinco comprobaciones contra la cadena, y ninguna se aceptó por
parecer plausible:

1. **Hay bytecode.** `eth_getCode` devuelve algo, no `0x`, en la fábrica y en el
   router.
2. **El contrato atestigua su identidad.** Al router se le pide su `factory()` y
   tiene que devolver esa misma fábrica ya confirmada, y su `WETH()` tiene que
   devolver el envuelto nativo que la red declara en el catálogo. Un contrato que
   dice ser el router de esa fábrica **es** ese router.
3. **El par está vivo.** `getPair(envuelto, stable)` devuelve un pool con
   `getReserves` positivas y un `token0` coherente.
4. **La comisión se mide, no se deduce.** El `getAmountsOut` del router tiene que
   reproducir **exactamente**, con aritmética entera, la fórmula de producto
   constante con una única comisión candidata — y con ninguna otra. Medido así:
   `997/1000` (30 bps) en los cinco DEX del 0,3 %, y `9975/10000` (**25 bps**) en
   PancakeSwap, cuya discrepancia entre repositorio y documentación queda
   resuelta por el contrato desplegado (ver `amm_protocols`).
5. **El ABI está en el dispatcher, y funciona.** Cada selector se buscó
   literalmente en el bytecode desplegado, y además se llamó: `getPair`,
   `getReserves`, `token0` y `getAmountsOut` contestaron datos coherentes con sus
   pools, y los tres `swap*` se llamaron con `eth_call` y revirtieron con una
   razón del cuerpo de la función —fondos, salida insuficiente—, que es lo que
   demuestra que el dispatcher lleva ese selector a la función que se cree.

Medido el 2026-10-09. Las redes que no se midieron **no están**: una dirección
puesta de memoria no falla al arrancar, falla al firmar.

## Un DEX por red, y por qué ése

Un par tiene **un** pool por DEX en V2 —no hay tramos de comisión que
comparar—, así que lo que varía por red es el DEX. Donde había más de un
candidato vivo se eligió el del pool envuelto/stable **más profundo**, medido:

- **Ethereum**: Uniswap V2 (4.061 WETH en el par USDC) sobre SushiSwap (55,8).
- **Polygon**: QuickSwap (6.968 WPOL en el par USDC) sobre SushiSwap (283).
- **Base**: Uniswap V2 (178,7 WETH) sobre SushiSwap (0,7).

Donde el candidato natural no existe, la tabla lo refleja:

- **Optimism**: el router de Uniswap V2 (`0x4A7b5Da6…`) devolvió **0 bytes** —la
  fábrica está, el router no—, así que la entrada es SushiSwap V2, que sí
  atestiguó identidad y comisión.
- **Arbitrum**: Camelot (fábrica `0x6EcCab42…`) quedó **fuera** aunque su
  router atestigua `factory()` y `WETH()`: sus funciones de swap no llevan los
  selectores estándar (`0x38ed1739`, `0x7ff36ab5`, `0x18cbafe5` ausentes del
  dispatcher medido), así que el calldata de este motor no lo ejecutaría.

Añadir otro DEX a una red que ya tiene uno es una entrada más aquí **y** su
comisión medida en `amm_protocols` — la coherencia entre las dos tablas se
comprueba al construir cada entrada, no se supone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from amigocompora.engines.amm_protocols import CONSTANT_FEES, FeeSource, parse_dex_id


@dataclass(frozen=True, slots=True)
class Deployment:
    """Los contratos de un DEX de estilo V2 en una red, ya verificados."""

    #: El identificador con el que `parse_dex_id` reconoce al protocolo
    #: (`"uniswap_v2"`, `"sushiswap_v2"`, `"quickswap_v2"`, `"pancakeswap_v2"`).
    #: De él salen la etiqueta del venue y la clave con la que el motor reconoce
    #: sus propias cotizaciones al construir.
    protocol: str
    #: Fábrica de pools. Se le pregunta por el pool de cada par.
    factory: str
    #: El contrato al que se manda el swap. Es también lo que devuelve
    #: `expected_destination`, y tiene que ser **el mismo** valor: el ejecutor
    #: compara el destino del payload contra lo que el motor declara.
    router: str
    #: Comisión del swap en puntos básicos, **medida** contra el despliegue.
    fee_bps: int

    def __post_init__(self) -> None:
        ref = parse_dex_id(self.protocol)
        constant = CONSTANT_FEES.get((ref.family, ref.version))
        if ref.fee_source is not FeeSource.PROTOCOL_CONSTANT or constant is None:
            raise ValueError(
                f"«{self.protocol}» no tiene una comisión constante medida en "
                f"amm_protocols: este motor publica la comisión que el router "
                f"cobra de verdad y no puede deducirla de su nombre"
            )
        if constant.value != self.fee_bps:
            raise ValueError(
                f"la tabla de direcciones dice {self.fee_bps} bps para "
                f"«{self.protocol}» y la constante medida dice {constant.value}: "
                f"las dos no pueden ser, y publicar una comisión que el router no "
                f"cobra envenena el impacto y la tabla de precios"
            )
        for name, address in (("factory", self.factory), ("router", self.router)):
            body = address.removeprefix("0x").lower()
            if len(body) != 40:
                raise ValueError(
                    f"la dirección de {name} de «{self.protocol}» no tiene 40 "
                    f"dígitos hexadecimales: «{address}»"
                )
            try:
                bytes.fromhex(body)
            except ValueError as error:
                raise ValueError(
                    f"la dirección de {name} de «{self.protocol}» no es "
                    f"hexadecimal: «{address}»"
                ) from error


#: **Medido el 2026-10-09** con las cinco comprobaciones del docstring del
#: módulo. Las direcciones se abrevian en los comentarios; la tabla las lleva
#: enteras porque es lo que se usa.
DEPLOYMENTS: Final[Mapping[str, Deployment]] = MappingProxyType(
    {
        "ethereum": Deployment(
            protocol="uniswap_v2",
            factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
            router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
            fee_bps=30,
        ),
        # El router de Uniswap V2 en Optimism no tiene bytecode: la entrada es
        # SushiSwap V2, que sí atestiguó identidad, par vivo y comisión.
        "optimism": Deployment(
            protocol="sushiswap_v2",
            factory="0xFBC12984689e5f15626Bad03Ad60160Fe98B303C",
            router="0x2ABf469074dc0b54d793850807E6eb5Faf2625b1",
            fee_bps=30,
        ),
        # Camelot quedó fuera: sin los selectores estándar en su dispatcher, el
        # calldata de este motor no se ejecutaría allí.
        "arbitrum": Deployment(
            protocol="sushiswap_v2",
            factory="0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
            router="0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
            fee_bps=30,
        ),
        "polygon": Deployment(
            protocol="quickswap_v2",
            factory="0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
            router="0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",
            fee_bps=30,
        ),
        "base": Deployment(
            protocol="uniswap_v2",
            factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
            router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
            fee_bps=30,
        ),
        # La comisión de PancakeSwap es la que se midió: 0,25 %, no el 0,3 %
        # de la familia ni el 0,2 % que sugería su repositorio.
        "bsc": Deployment(
            protocol="pancakeswap_v2",
            factory="0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
            router="0x10ED43C718714eb63d5aA57B78B54704E256024E",
            fee_bps=25,
        ),
    }
)

#: Las redes en las que este motor construye swaps. Es exactamente lo que hay en
#: la tabla, sin una lista paralela que pueda desincronizarse.
SWAP_CHAINS: Final[frozenset[str]] = frozenset(DEPLOYMENTS)
