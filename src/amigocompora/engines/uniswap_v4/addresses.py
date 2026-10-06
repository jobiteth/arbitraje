"""Dónde está cada contrato de Uniswap V4, **medido red por red**.

## V4 no tiene fábrica, y eso cambia qué hay que verificar

En V3 la prueba de identidad era preguntarle a la fábrica por un pool y comprobar
que el pool devuelto fuera coherente. Aquí **no hay a quién preguntar**: un pool
de V4 no es un contrato, es una entrada en el almacén de un único `PoolManager`
cuya clave se deriva. Así que la verificación cambió de forma, y fue esta:

1. **Hay bytecode** en cada dirección, y con el tamaño de un contrato de verdad
   —no una dirección con cuatro bytes, que es el fallo que se ve al copiar de un
   listado—. Medido: V4Quoter 5820 bytes, StateView 3531, PositionManager 23877,
   UniversalRouter 19499, PoolManager 24009.
2. **El contrato atestigua a quién sirve.** Los tres periféricos exponen
   `poolManager()`, que es un *getter* de la dirección del singleton. Se llamó en
   cada red y los tres devolvieron **la misma** dirección de la tabla: esa es la
   comprobación que no se puede falsificar copiando una lista, porque una
   dirección equivocada devuelve basura o nada.
3. **El ABI está en el dispatcher.** Igual que en V3 y por el mismo motivo: se
   busca el selector de 4 bytes **dentro del bytecode desplegado** en vez de
   fiarse de la firma recordada. Ver `calldata.py`.

## Por qué las direcciones van en minúsculas

No es descuido. Salen así de la medición y se copian **tal cual**: reescribirlas a
mano con su *checksum* es introducir una oportunidad de teclear mal un carácter
sin ganar nada, porque la cadena no distingue la caja en una dirección. El día que
una de estas esté mal, el fallo tiene que ser «este contrato no responde», no
«esta letra era mayúscula».

## Las tres direcciones que están, y la que no

El motor sólo necesita **tres** contratos por red, y son los tres que se guardan:

- el `PoolManager`, que es el singleton donde vive el estado de todos los pools;
- el `V4Quoter`, que cotiza por `eth_call` —gratis y sin clave—;
- el `UniversalRouter`, que es a donde se manda el swap.

El `PositionManager` (23877 bytes, medido) **no está** porque no se usa: sirve para
aportar y retirar liquidez, y este motor no hace ninguna de las dos cosas.
Guardarlo sería una dirección que nadie lee y que envejece sin que nada avise.

`PERMIT2` sí está, y es el mismo en todas las redes porque se desplegó con
`CREATE2` en una dirección determinista. No es un detalle: **el Universal Router
no mueve el token él mismo**, se lo pide a Permit2, así que un swap con token
exige **dos** permisos encadenados —el del ERC-20 a Permit2 y el de Permit2 al
router— y ninguno sustituye al otro. Medido: intentar el swap sin ellos devuelve
`AllowanceExpired` de Permit2, no un error del router.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

#: Permit2. La misma dirección en todas las redes donde se midió, porque se
#: desplegó con CREATE2. El Universal Router cobra a través de él.
PERMIT2: Final = "0x000000000022d473030f116ddee9f6b43ac78ba3"

#: Los tramos que se **prueban**, como parejas `(comisión, espaciado de ticks)`.
#:
#: En V4 no hay lista que consultar: el LP elige la comisión y el espaciado
#: libremente y el `poolId` sale de esos dos números, así que esto es lo que se
#: pregunta, no lo que existe. Se probaron estos cuatro en las siete redes y el
#: resultado desmiente la convención heredada de V3: **Optimism sólo tiene el
#: 0,05 %** y **Unichain sólo el 0,30 %**, mientras que en **Base el profundo es
#: el 0,30 %** y no el 0,05 %. Suponer los cuatro habría dejado a Optimism y
#: Unichain sin ninguna cotización.
#:
#: La comisión va en **centésimas de punto básico** —`3000` es el 0,30 %—, que es
#: la unidad de V4; la del dominio son puntos básicos. Ver `_fee_from_venue`.
#:
#: Un pool con un espaciado que no sea el canónico de su comisión es invisible
#: para este motor. Se dice en vez de suponer que no hay ninguno.
FEE_TIERS: Final[tuple[tuple[int, int], ...]] = (
    (100, 1),
    (500, 10),
    (3_000, 60),
    (10_000, 200),
)


@dataclass(frozen=True, slots=True)
class Deployment:
    """Los contratos de Uniswap V4 en una red, ya verificados."""

    #: El singleton donde vive el estado de **todos** los pools.
    pool_manager: str
    #: StateView. Lee el estado de un pool por su `poolId`: es lo que dice si el
    #: pool existe y si tiene liquidez, que es la pregunta que en V3 le hacía la
    #: fábrica. Cotizar con el quoter un pool vacío revienta sin decir por qué; el
    #: StateView lo dice con un número.
    state_view: str
    #: V4Quoter. Cotiza por `eth_call`, que no cuesta gas: de ahí que este motor
    #: no necesite clave ni gastar nada para dar un precio.
    quoter: str
    #: A dónde se manda el swap. Es también lo que devuelve `expected_destination`,
    #: y tiene que ser **el mismo** valor: el camino de ejecución compara el
    #: destino del payload contra lo que el motor declara antes de firmar.
    universal_router: str


#: **Medido el 2026-10-06** con las tres comprobaciones del docstring del módulo:
#: bytecode con tamaño de contrato, `poolManager()` coincidente en los tres
#: periféricos, y los selectores presentes en el dispatcher.
#:
#: Añadir una red es medirla. Una dirección puesta de memoria no falla al
#: arrancar: falla al firmar, que es el peor sitio para enterarse.
DEPLOYMENTS: Final[Mapping[str, Deployment]] = MappingProxyType(
    {
        "ethereum": Deployment(
            pool_manager="0x000000000004444c5dc75cb358380d2e3de08a90",
            state_view="0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
            quoter="0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
            universal_router="0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
        ),
        "optimism": Deployment(
            pool_manager="0x9a13f98cb987694c9f086b1f5eb990eea8264ec3",
            state_view="0xc18a3169788f4f75a170290584eca6395c75ecdb",
            quoter="0x1f3131a13296fb91c90870043742c3cdbff1a8d7",
            universal_router="0x851116d9223fabed8e56c0e6b8ad0c31d98b3507",
        ),
        "arbitrum": Deployment(
            pool_manager="0x360e68faccca8ca495c1b759fd9eee466db9fb32",
            state_view="0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
            quoter="0x3972c00f7ed4885e145823eb7c655375d275a1c5",
            universal_router="0xa51afafe0263b40edaef0df8781ea9aa03e381a3",
        ),
        "polygon": Deployment(
            pool_manager="0x67366782805870060151383f4bbff9dab53e5cd6",
            state_view="0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a",
            quoter="0xb3d5c3dfc3a7aebff71895a7191796bffc2c81b9",
            universal_router="0x1095692a6237d83c6a72f3f5efedb9a670c49223",
        ),
        "base": Deployment(
            pool_manager="0x498581ff718922c3f8e6a244956af099b2652b2b",
            state_view="0xa3c0c9b65bad0b08107aa264b0f3db444b867a71",
            quoter="0x0d5e0f971ed27fbff6c2837bf31316121532048d",
            universal_router="0x6ff5693b99212da76ad316178a184ab56d299b43",
        ),
        "bsc": Deployment(
            pool_manager="0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
            state_view="0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
            quoter="0x9f75dd27d6664c475b90e105573e550ff69437b0",
            universal_router="0x1906c1d672b88cd1b9ac7593301ca990f94eae07",
        ),
        "unichain": Deployment(
            pool_manager="0x1f98400000000000000000000000000000000004",
            state_view="0x86e8631a016f9068c3f085faf484ee3f5fdee8f2",
            quoter="0x333e3c607b141b18ff6de9f258db6e77fe7491e0",
            universal_router="0xef740bf23acae26f6492b10de645d6b98dc8eaf3",
        ),
    }
)

#: Las redes en las que este motor construye swaps. Es exactamente lo que hay en
#: la tabla, sin una lista paralela que pueda desincronizarse.
SWAP_CHAINS: Final[frozenset[str]] = frozenset(DEPLOYMENTS)
