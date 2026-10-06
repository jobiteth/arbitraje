"""Dónde está cada contrato de Uniswap V3, **medido red por red**.

## Cómo se midió, y por qué no se copió de una lista

Cada dirección de esta tabla pasó tres comprobaciones contra la cadena, y
ninguna se aceptó por parecer plausible:

1. **Hay bytecode.** `eth_getCode` devuelve algo, no `0x`.
2. **El contrato atestigua su propia identidad.** La fábrica no se cree por su
   dirección: se le pide `getPool(WETH, USDC, 500)` y se comprueba que el pool
   devuelto tenga `token0`, `token1` y `fee` exactamente los que se pidieron. Un
   contrato que devuelve un pool coherente **es** la fábrica de V3 para ese par.
   El router se comprueba por su `factory()`, que tiene que devolver esa misma
   fábrica ya confirmada, y su `WETH9()`, que tiene que devolver el envuelto de
   la red.
3. **El ABI está en el dispatcher.** Un selector mal recordado llama a otra
   función del mismo contrato —o revierte— y no se nota hasta que hay dinero
   dentro. Así que en vez de fiarse de la firma, se busca su selector de 4 bytes
   **dentro del bytecode desplegado**: el dispatcher de Solidity lo lleva
   literal. Medido así, y con el resultado que separa dos mundos:

   | contrato | `exactInputSingle` con `deadline` (`0x414bf389`) | sin él (`0x04e45aaf`) |
   |---|---|---|
   | SwapRouter V1 `0xE592427A…` | **sí** en Ethereum, Optimism, Arbitrum, Polygon | no |
   | SwapRouter02 `0x68b34658…` / `0x2626664c…` / `0xB971eF87…` | no | **sí** en las seis |

## Las dos formas de mover el token, que es lo que decide la tabla

Uniswap V3 tiene **dos** routers y no son intercambiables desde el punto de vista
de la custodia:

- **SwapRouter V1** (`0xE592427A…`) mueve el token él mismo con `transferFrom`.
  Basta autorizarle el ERC-20 a él: **un permiso**. Además su
  `exactInputSingle` lleva `deadline`, así que el payload **caduca** — un
  payload viejo no se puede ejecutar a un precio que ya no existe.
- **SwapRouter02** no toca el token: se lo pide a **Permit2**. Hacen falta **dos
  permisos encadenados** y ninguno sustituye al otro. Y su `exactInputSingle`
  **no** tiene `deadline`: no hay caducidad en el payload, y la protección
  contra un precio viejo queda del lado de la comprobación de deriva del motor y
  del diálogo de confirmación.

De ahí la regla de la tabla, que no es la más cómoda sino la más simple de
custodia: **se usa V1 donde está desplegado** —un permiso en vez de dos, y con
caducidad— y **SwapRouter02 sólo donde es el único que existe**, que medido es
**Base y BSC**.

En Base el SwapRouter V1 **no está desplegado**: la dirección que tiene ese
número allí devuelve 2109 bytes y su `factory()` es ilegible, o sea que es otro
contrato. Eso no es un detalle: Base es la red de la prueba, y significa que el
camino de «un solo `approve` al router» **no existe** ahí. Por eso el permiso
encadenado no es una función opcional del motor, es la que hace que la compra
funcione.

## Las redes que no están

`avalanche` y `unichain` **no aparecen**: las direcciones candidatas que se
probaron allí devolvieron 0 bytes, así que no se sabe dónde están sus contratos
y no se inventan. Añadir una red es medirla y añadir su entrada; el motor lee
esta tabla y no tiene ninguna lista propia, así que no puede declarar una red
que no se haya comprobado.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

#: Permit2. Es el mismo en **todas** las redes donde se midió (9152 bytes en las
#: seis), porque se desplegó con CREATE2 en una dirección determinista. Se
#: escribe aquí una vez y cada entrada de la tabla la referencia.
PERMIT2: Final = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

#: Los tramos de comisión que se **sondean** en la fábrica, en centésimas de
#: punto básico: 0,01 %, 0,05 %, 0,30 % y 1 %.
#:
#: No son un conjunto cerrado —`enableFeeAmount` admite cualquiera por debajo de
#: `1000000`— y por eso esto es la lista de lo que se pregunta y no la lista de
#: lo que existe. Un pool en un tramo que no esté aquí es invisible para este
#: motor; se dice en vez de suponer que no hay ninguno. Se incluye el 1 % porque
#: es donde resultó estar el único pool del token que motivó todo esto.
FEE_TIERS: Final[tuple[int, ...]] = (100, 500, 3_000, 10_000)


class Approval(StrEnum):
    """Cómo hay que autorizar el token para que ese router pueda moverlo."""

    #: El router hace `transferFrom`: un `approve` del ERC-20 contra él.
    DIRECT = "direct"
    #: El router cobra vía Permit2: dos permisos encadenados.
    PERMIT2 = "permit2"


@dataclass(frozen=True, slots=True)
class Deployment:
    """Los contratos de Uniswap V3 en una red, ya verificados."""

    #: Fábrica de pools. Se le pregunta por el pool de cada par y tramo.
    factory: str
    #: QuoterV2. Cotiza por `eth_call`, que no cuesta gas: es lo que permite
    #: cotizar sin clave y sin gastar nada.
    quoter: str
    #: El contrato al que se manda el swap. Es también lo que devuelve
    #: `expected_destination`, y tiene que ser **el mismo** valor: el ejecutor
    #: compara el destino del payload contra lo que el motor declara.
    router: str
    #: Cómo se autoriza el token contra ese router.
    approval: Approval
    #: `exactInputSingle` de este router lleva `deadline` en la struct.
    #: Verdadero en V1, falso en SwapRouter02 — comprobado por selector.
    has_deadline: bool
    #: Permit2 de la red, sólo si el router cobra a través de él.
    permit2: str | None = None

    def __post_init__(self) -> None:
        if self.approval is Approval.PERMIT2 and not self.permit2:
            raise ValueError(
                f"un despliegue con permiso encadenado necesita la dirección de "
                f"Permit2: sin ella no se sabe a quién autorizar el token "
                f"(router {self.router})"
            )
        if self.approval is Approval.DIRECT and self.permit2:
            raise ValueError(
                f"un despliegue de permiso directo no usa Permit2 "
                f"(router {self.router}): declararlo haría que el ejecutor "
                f"autorizara al contrato equivocado"
            )


#: Direcciones de SwapRouter02, el único router V3 de Base y BSC. Se nombran
#: aparte porque varias redes comparten contrato y así se ve de un vistazo.
_SR02_MAINNET: Final = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
_SR02_BASE: Final = "0x2626664c2603336E57B271c5C0b26F421741e481"
_SR02_BSC: Final = "0xB971eF87ede563556b2ED4b1C0B0019111Dd85d2"
_V1: Final = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
_Q2_MAINNET: Final = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
_FACTORY_MAINNET: Final = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

#: **Medido el 2026-10-06**, con las tres comprobaciones del docstring del
#: módulo. Las seis redes de abajo devolvieron un pool coherente al pedirle
#: `getPool(WETH, <stable>, 500)` a su fábrica.
#:
#: Añadir una red aquí es haberla medido. Una dirección puesta de memoria no
#: falla al arrancar: falla al firmar, que es el peor sitio para enterarse.
DEPLOYMENTS: Final[Mapping[str, Deployment]] = MappingProxyType(
    {
        "ethereum": Deployment(
            factory=_FACTORY_MAINNET,
            quoter=_Q2_MAINNET,
            router=_V1,
            approval=Approval.DIRECT,
            has_deadline=True,
        ),
        "optimism": Deployment(
            factory=_FACTORY_MAINNET,
            quoter=_Q2_MAINNET,
            router=_V1,
            approval=Approval.DIRECT,
            has_deadline=True,
        ),
        "arbitrum": Deployment(
            factory=_FACTORY_MAINNET,
            quoter=_Q2_MAINNET,
            router=_V1,
            approval=Approval.DIRECT,
            has_deadline=True,
        ),
        "polygon": Deployment(
            factory=_FACTORY_MAINNET,
            quoter=_Q2_MAINNET,
            router=_V1,
            approval=Approval.DIRECT,
            has_deadline=True,
        ),
        # En Base el SwapRouter V1 no está desplegado: SwapRouter02 es el único.
        "base": Deployment(
            factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
            quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
            router=_SR02_BASE,
            approval=Approval.PERMIT2,
            has_deadline=False,
            permit2=PERMIT2,
        ),
        # En BSC tampoco: medido, `0xE592427A…` no está allí.
        "bsc": Deployment(
            factory="0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
            quoter="0x78D78E420Da98ad378D7799bE8f4AF69033EB077",
            router=_SR02_BSC,
            approval=Approval.PERMIT2,
            has_deadline=False,
            permit2=PERMIT2,
        ),
    }
)

#: Las redes en las que este motor construye swaps. Es exactamente lo que hay en
#: la tabla, sin una lista paralela que pueda desincronizarse.
SWAP_CHAINS: Final[frozenset[str]] = frozenset(DEPLOYMENTS)
