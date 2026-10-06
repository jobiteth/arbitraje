"""Identificación de protocolos AMM y de dónde sale su comisión.

Las fuentes nombran al mismo protocolo de maneras distintas y ni siquiera son
consistentes consigo mismas. Medido en GeckoTerminal, Uniswap V3 aparece como
`uniswap_v3` en Ethereum, `uniswap-v3-base` en Base, `uniswap_v3_arbitrum` en
Arbitrum y `uniswap_v3_polygon_pos` en Polygon: separador distinto y sufijo de
red a veces sí y a veces no. Normalizar eso en un sitio evita que cada motor
lleve su propia lista de casos especiales, y es lo que permite que **añadir un
protocolo sea una entrada en una tabla**.

---

### El dato que de verdad importa: de dónde sale la comisión

Esto no es taxonomía por gusto. La comisión decide cuánto se recibe en un swap,
y según el protocolo se conoce de tres maneras muy distintas. Verificado en los
contratos, que es la fuente última:

- **Constante del protocolo.** Uniswap V1 (`uniswap_exchange.vy`:
  `input_amount * 997`) y V2 (`UniswapV2Library`: `amountIn.mul(997)`) cobran
  30 bps fijos, escritos en el contrato. Sushiswap V2 hace lo mismo
  (`amount0In.mul(3)` sobre 1000). Aquí la versión del protocolo **es** la
  comisión, y se puede deducir sin preguntar.
- **Elegida al crear el pool.** Uniswap V3 arranca con los tramos 500, 3000 y
  10000 centésimas de punto básico (`UniswapV3Factory`), pero `enableFeeAmount`
  admite cualquiera por debajo de `1000000`: **no es un conjunto cerrado**. La
  familia Solidly (Velodrome, Aerodrome) la lee del factory pool a pool
  (`IPoolFactory(factory).getFee(address(this), stable)`). En ambos casos hay
  que leer la comisión de ese pool concreto; deducirla de la versión sería
  inventarla.
- **Decidida en cada swap.** Uniswap V4 guarda la comisión en 24 bits
  (`LPFeeLibrary`: `MAX_LP_FEE = 1000000`) y reserva el valor `0x800000` como
  `DYNAMIC_FEE_FLAG`: un *hook* puede sobrescribir la comisión en cada
  operación. Para esos pools **la comisión no es conocible sin simular el
  hook**, y `getInitialLPFee` devuelve 0 precisamente en ellos. De ahí la regla
  de `accepts_reported_fee()`: en un protocolo con hooks, una comisión
  publicada de 0 no significa «gratis», significa «dinámica», y el pool se
  descarta.

PancakeSwap V2 no está en la tabla de constantes a propósito: su contrato en
repositorio usa `amount0In.mul(2)` (20 bps) mientras su documentación publica
0,25 %, y esa discrepancia no se resuelve sin leer el bytecode desplegado.
Mientras no se resuelva, sus pools se cotizan con la comisión que publique la
fuente, como cualquier otro protocolo de comisión por pool.

El caso por omisión, que es el más frecuente, es el segundo: todo protocolo que
no esté en la tabla de constantes y no lleve hooks se trata como comisión **por
pool**. No es una suposición, es la ausencia de una: significa «no deduzco nada
de su nombre, que la fuente publique la comisión de este pool o no lo cotizo».
Esa es la rama por la que entran Raydium, Orca, Curve o Aerodrome con su
comisión real, y la que evita a la vez atribuirles 30 bps por parecerse a un V2.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from amigocompora.domain.chains import CHAINS
from amigocompora.domain.models import Venue, VenueKind
from amigocompora.domain.money import BasisPoints


class FeeSource(StrEnum):
    """De dónde se puede saber la comisión de un pool de este protocolo."""

    #: Fijada en el contrato: igual en todos los pools del protocolo.
    PROTOCOL_CONSTANT = "protocol_constant"
    #: Propia de cada pool: hay que leerla de ese pool, no se deduce del nombre.
    #: Es el caso por omisión de todo protocolo que no sea de comisión constante
    #: ni lleve hooks.
    PER_POOL = "per_pool"
    #: Un hook puede cambiarla en cada swap: puede no ser conocible.
    PER_SWAP = "per_swap"
    #: Ni siquiera hay identificador de protocolo que examinar.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProtocolRef:
    """Un protocolo AMM identificado, con su política de comisiones."""

    family: str
    version: str
    label: str
    fee_source: FeeSource
    #: Comisión del protocolo, sólo si es una constante verificada.
    constant_fee: BasisPoints | None = None

    @property
    def key(self) -> str:
        """Clave estable del protocolo, ya sin el sufijo de red."""
        return f"{self.family}-{self.version}" if self.version else self.family

    @property
    def is_quotable(self) -> bool:
        """¿Se puede cotizar un pool de este protocolo con honestidad?

        `UNKNOWN` es el identificador vacío: sin nombre de protocolo no hay nada
        sobre lo que razonar. Todo lo demás es cotizable **a condición de que la
        comisión venga de algún sitio verificable**, que es lo que decide
        `accepts_reported_fee`.
        """
        return self.fee_source is not FeeSource.UNKNOWN

    def accepts_reported_fee(self, fee: BasisPoints) -> bool:
        """¿Es creíble esta comisión publicada para este protocolo?

        La regla operativa es una sola: **una comisión publicada de 0 no se
        acepta nunca.** No existe un AMM real que no cobre nada, así que un 0
        publicado siempre significa «aquí no hay dato», y el caso en que eso es
        demostrable es Uniswap V4: `getInitialLPFee` devuelve exactamente 0 para
        los pools de comisión dinámica, donde la decide un hook en cada
        operación. Cotizar uno de esos con 0 prometería un precio que nadie va a
        recibir.

        En un protocolo de comisión constante da igual lo que publique la
        fuente: se usa la constante del contrato, que es mejor dato.
        """
        if self.fee_source is FeeSource.PROTOCOL_CONSTANT:
            return True
        return fee.value > 0


#: Comisión total del swap cuando es una constante del protocolo, en puntos
#: básicos, **verificada en el contrato**. La referencia de cada una está en el
#: docstring del módulo.
#:
#: Añadir una entrada exige leer el contrato del protocolo. Un valor puesto de
#: memoria convierte una cifra medida en una inventada sin que se note.
CONSTANT_FEES: Final[Mapping[tuple[str, str], BasisPoints]] = {
    ("uniswap", "v1"): BasisPoints(30),
    ("uniswap", "v2"): BasisPoints(30),
    ("sushiswap", "v2"): BasisPoints(30),
}

#: Marcas de que un protocolo admite hooks que alteran la comisión por swap.
#: `infinity` es el nombre de la versión con hooks de PancakeSwap.
_HOOKED_MARKS: Final = frozenset({"v4", "infinity"})

_VERSION = re.compile(r"^v(\d+)$")

#: Palabras que son el nombre de la red y no del protocolo, y que por tanto se
#: quitan del identificador. Se derivan del registro de redes para que añadir
#: una red no obligue a tocar esta lista.
_CHAIN_WORDS: Final[frozenset[str]] = (
    frozenset(word for key in CHAINS for word in re.split(r"[-_]", key))
    | frozenset(spec.native_symbol.lower() for spec in CHAINS.values())
    # Alias con los que las fuentes nombran a estas mismas redes.
    | frozenset({"eth", "pos", "matic", "bnb", "avax", "arb", "op", "mainnet"})
)


def parse_dex_id(dex_id: str) -> ProtocolRef:
    """Identifica el protocolo a partir del identificador de una fuente.

    Tolerante a propósito: un identificador que no se reconoce no es un error,
    es un protocolo que todavía no conocemos. Se devuelve con `fee_source`
    `UNKNOWN` para que el motor decida —y lo que decide es omitirlo.
    """
    words = [word for word in re.split(r"[-_]", dex_id.strip().lower()) if word]
    if not words:
        return ProtocolRef(family="", version="", label="", fee_source=FeeSource.UNKNOWN)

    version, rest = _split_version(words)
    family_words = [word for word in rest if word not in _CHAIN_WORDS] or rest
    family = "-".join(family_words)
    marks = frozenset(words)

    fee_source, constant_fee = _classify(family, version, marks)
    return ProtocolRef(
        family=family,
        version=version,
        label=_label(family_words, version),
        fee_source=fee_source,
        constant_fee=constant_fee,
    )


def _split_version(words: list[str]) -> tuple[str, list[str]]:
    """Separa la versión del resto. `["traderjoe","v2","2"]` → `("v2.2", …)`.

    Algunas fuentes publican la versión menor como una palabra más
    (`traderjoe-v2-2`), así que tras encontrar `vN` se absorben los números
    sueltos que la siguen.
    """
    for index, word in enumerate(words):
        match = _VERSION.match(word)
        if match is None:
            continue
        minors: list[str] = []
        end = index + 1
        while end < len(words) and words[end].isdigit():
            minors.append(words[end])
            end += 1
        version = f"v{'.'.join([match.group(1), *minors])}"
        return version, words[:index] + words[end:]
    return "", words


def _classify(
    family: str,
    version: str,
    marks: frozenset[str],
) -> tuple[FeeSource, BasisPoints | None]:
    """Decide de dónde sale la comisión de este protocolo.

    El orden va de lo más verificado a lo menos. La última rama —comisión por
    pool para todo lo que no se reconoce— no es resignación: es la respuesta
    correcta. Un protocolo que no está en la tabla de constantes **no tiene**
    una comisión deducible de su nombre, así que la única forma honesta de
    cotizarlo es exigir que la fuente publique la de ese pool concreto. Eso es
    justo lo que significa `PER_POOL`, y lo que hace que Raydium, Orca, Curve o
    Aerodrome se coticen con su comisión real en vez de quedar fuera.

    Suponer 30 bps porque «los V2 cobran 30» sigue siendo el error que este
    módulo existe para no cometer: `PER_POOL` no supone nada, exige el dato.
    """
    constant = CONSTANT_FEES.get((family, version))
    if constant is not None:
        return FeeSource.PROTOCOL_CONSTANT, constant
    if marks & _HOOKED_MARKS:
        return FeeSource.PER_SWAP, None
    return FeeSource.PER_POOL, None


def _label(family_words: list[str], version: str) -> str:
    """Nombre legible: `["uniswap"], "v3"` → `"Uniswap V3"`."""
    pretty = " ".join(word.capitalize() for word in family_words)
    return f"{pretty} {version.upper()}" if version else pretty


def venue_for(protocol: ProtocolRef, fee: BasisPoints, chain_key: str) -> Venue:
    """Construye el venue de un pool, con su comisión dentro de la identidad.

    El *fee tier* forma parte del venue y no es sólo una columna de la tabla.
    Uniswap V3 publica el mismo par en varios pools con comisiones distintas
    (0,01 %, 0,05 %, 0,30 %…) y cada uno tiene su propia profundidad y su propio
    precio de ejecución. Son sitios distintos donde operar, no uno con tres
    precios: medido con datos reales, WETH/USDC en Ethereum sale en tres pools
    V3 con 32 bps de diferencia entre el mejor y el peor. Darles el mismo
    `venue_id` haría que la tabla mostrara tres filas idénticas y que un
    análisis de discrepancias las cruzara entre sí como si fueran el mismo sitio.

    Vive aquí y no en cada motor para que el identificador sea **el mismo** lo
    construya quien lo construya: dos motores que miran el mismo pool tienen que
    producir el mismo `venue_id`, o la caché y la comparación verían dos sitios
    donde hay uno.
    """
    return Venue(
        venue_id=f"{protocol.key}@{fee.value}",
        name=f"{protocol.label} {fee.as_percent():f} %",
        kind=VenueKind.DEX,
        chain=chain_key,
    )
