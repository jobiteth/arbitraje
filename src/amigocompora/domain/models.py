"""Entidades y value objects del dominio.

Todo es inmutable (`frozen=True`) y se valida en construcción: un objeto que
existe es un objeto coherente, así que las capas superiores no vuelven a
comprobar invariantes. Las cantidades usan siempre `TokenAmount`/`Decimal`,
nunca `float` (ver `money`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError
from amigocompora.domain.money import EXACT, BasisPoints, Price, TokenAmount

#: Margen que se tolera en la suma de probabilidades de un mercado de predicción
#: antes de considerarla incoherente. 50 bps = 0,5 %: por encima de eso la
#: desviación ya no se explica por redondeo de precios.
DEFAULT_OVERROUND_TOLERANCE: Final = BasisPoints(50)


def _require_aware(moment: datetime, field_name: str) -> None:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise InvalidAmountError(f"{field_name} debe llevar zona horaria, llegó {moment!r}")


# --------------------------------------------------------------------------- #
# Redes y tokens
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Token:
    """Un token en una red concreta. `address=None` = token nativo de la red.

    `chain` es la **clave** de la red en el registro (`"ethereum"`, `"solana"`),
    no un chain id numérico: ver `domain.chains` para por qué. El dominio no
    valida que la clave exista, porque validarla exigiría importar el registro
    y un token es un dato, no una consulta.
    """

    symbol: str
    decimals: int
    chain: str
    address: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise InvalidAmountError("el token necesita un symbol")
        if self.decimals < 0:
            raise InvalidAmountError(f"decimals debe ser >= 0, llegó {self.decimals}")
        if not self.chain:
            raise InvalidAmountError(f"el token {self.symbol} necesita una red")

    @property
    def is_native(self) -> bool:
        return self.address is None

    def is_same_asset(self, other: Token) -> bool:
        """Si `other` es **este mismo token**, no sólo uno que se llama igual.

        El símbolo no identifica un token: dos contratos distintos pueden
        publicar el mismo. El caso está medido, no supuesto —en Polygon el USDC
        nativo (`0x3c49…3359`) y el USDC puenteado, «USDC.e» (`0x2791…4174`),
        devuelven los dos `symbol() == "USDC"`—, y son tokens distintos: viven en
        piscinas distintas y sólo uno de los dos lo acepta Polymarket como
        colateral. Decidir por símbolo no sólo haría imposible expresar el par
        que los cambia, sino que volvería indistinguibles dos cosas que no lo
        son en todo lo que compare por nombre.

        Lo que identifica es la dirección, y el token nativo no tiene: dos
        nativos de la misma red con el mismo símbolo sí son el mismo token.
        La dirección se compara sin distinguir mayúsculas porque el *checksum*
        EIP-55 es una convención de presentación, no parte de la identidad —
        la misma regla que ya aplica `domain.chains`.
        """
        if self.chain != other.chain or self.symbol != other.symbol:
            return False
        if self.address is None or other.address is None:
            return self.address is None and other.address is None
        return self.address.lower() == other.address.lower()

    @property
    def qualified_symbol(self) -> str:
        """El símbolo acompañado de lo que hace falta para desempatar homónimos.

        Sólo se usa cuando hay que distinguir —los dos lados de un par que se
        llaman igual, o dos entradas del mismo nombre en un desplegable—, así que
        no tiene que ser bonito: tiene que bastar para saber cuál es cuál. Diez
        caracteres de dirección separan el USDC nativo de Polygon del puenteado,
        que es el caso que existe. Un token nativo no tiene dirección, y ahí lo
        que desempata es decir que lo es.
        """
        if self.address is None:
            return f"{self.symbol} (nativo)"
        return f"{self.display_symbol} {self.address[:10]}…"

    @property
    def display_symbol(self) -> str:
        """El nombre con el que se enseña el token, cuando el símbolo engaña.

        El USDC puenteado de Polygon publica `USDC`, igual que el nativo; se enseña
        como «USDC.e», que es como lo llama el mercado. El símbolo que usa el
        cálculo no cambia: sólo cambia lo que ve el usuario.
        """
        if self.address is None:
            return self.symbol
        nombres = {("polygon", "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"): "USDC.e"}
        return nombres.get((self.chain, self.address.lower()), self.symbol)

    def amount(self, value: Decimal | int | str) -> TokenAmount:
        """Construye una cantidad de este token desde unidades humanas."""
        return TokenAmount.from_decimal(value, self.decimals, self.symbol)

    def zero(self) -> TokenAmount:
        return TokenAmount.zero(self.decimals, self.symbol)


@dataclass(frozen=True, slots=True)
class TradingPair:
    """Par `base/quote`. El precio se expresa siempre en unidades de `quote`."""

    base: Token
    quote: Token

    def __post_init__(self) -> None:
        if self.base.is_same_asset(self.quote):
            raise CurrencyMismatchError(f"un par no puede ser {self.base.symbol} contra sí mismo")
        if self.base.chain != self.quote.chain:
            raise CurrencyMismatchError(
                f"{self.base.symbol} está en la red {self.base.chain} "
                f"y {self.quote.symbol} en {self.quote.chain}: no hay pool que los una"
            )

    @property
    def symbol(self) -> str:
        """Etiqueta del par para la tabla, el diálogo y el registro.

        Con símbolos distintos es lo obvio —`USDC/WETH`—. Con el mismo símbolo en
        los dos lados no lo es: `USDC/USDC` no dice qué se cambia por qué, ni
        sirve para leer después el registro de ejecuciones. Ahí se añade el
        principio de la dirección, que es lo único que distingue a los dos.
        """
        if self.base.symbol != self.quote.symbol:
            return f"{self.base.symbol}/{self.quote.symbol}"
        return f"{self.base.qualified_symbol}/{self.quote.qualified_symbol}"

    @property
    def chain(self) -> str:
        return self.base.chain

    def __str__(self) -> str:
        return self.symbol


# --------------------------------------------------------------------------- #
# Lugares de negociación
# --------------------------------------------------------------------------- #
class VenueKind(StrEnum):
    DEX = "dex"
    PREDICTION_MARKET = "prediction_market"


@dataclass(frozen=True, slots=True)
class Venue:
    """Un exchange descentralizado o un mercado de predicción."""

    venue_id: str
    name: str
    kind: VenueKind
    chain: str

    def __post_init__(self) -> None:
        if not self.venue_id:
            raise InvalidAmountError("el venue necesita un venue_id")


# --------------------------------------------------------------------------- #
# Cotizaciones y comparación
# --------------------------------------------------------------------------- #
class Measurement(StrEnum):
    """Cómo se ha obtenido una cifra de una cotización.

    Existe porque las fuentes reales **no publican lo mismo**. DexScreener da
    las reservas exactas de cada pool pero no su comisión; GeckoTerminal da la
    comisión exacta pero sólo la liquidez agregada en dólares. Si las dos cosas
    se presentaran como equivalentes, el usuario no podría saber cuál de los
    números de la pantalla es una medida y cuál un supuesto nuestro.

    Decidir con una estimación es legítimo; decidir creyendo que es una medida,
    no. Por eso cada cotización dice de qué tipo es cada cifra, y la UI lo
    marca.
    """

    #: La fuente publica el valor tal cual.
    REPORTED = "reported"
    #: Calculado por nosotros sobre valores publicados, sin supuestos añadidos
    #: (p. ej. el impacto de precio a partir de las reservas reales del pool).
    DERIVED = "derived"
    #: Aproximado mediante un supuesto documentado en el motor que lo produce.
    ESTIMATED = "estimated"

    @property
    def label(self) -> str:
        return _MEASUREMENT_LABELS[self]

    @property
    def is_exact(self) -> bool:
        return self is not Measurement.ESTIMATED


_MEASUREMENT_LABELS: Final[dict[Measurement, str]] = {
    Measurement.REPORTED: "publicado por la fuente",
    Measurement.DERIVED: "calculado sobre datos publicados",
    Measurement.ESTIMATED: "estimado con un supuesto",
}


@dataclass(frozen=True, slots=True)
class Quote:
    """Resultado de cotizar «vender `amount_in` de base por quote» en un venue.

    `amount_out` es **neto**: ya incorpora la comisión y el impacto de precio.
    Esa es la cifra con la que se compara, y está siempre.

    ---

    ### Por qué `fee_bps` puede ser `None`

    Hay fuentes que dan el resultado de la ejecución sin desglosarlo. El caso
    medido es Jupiter, el agregador de Solana: devuelve el `outAmount` exacto en
    unidades raw y el impacto de precio que ha medido, pero cada tramo de su
    ruta expone sólo `['ammKey','inAmount','inputMint','label','outAmount',
    'outputMint','updateContextSlot']` — comprobado contra la API, **sin
    `feeAmount`**. La comisión está cobrada dentro de `outAmount` y no es
    atribuible por separado.

    Las tres formas de contarlo eran: publicar 0, estimarla, o decir que no se
    sabe. Las dos primeras están descartadas por el proyecto —«una comisión
    publicada de 0 no se acepta nunca», ver `engines.amm_protocols`, y estimar
    aquí exigiría un precio de referencia de otra fuente—, así que queda la
    tercera. `None` significa exactamente «la fuente no la desglosa», no
    «gratis» ni «aproximadamente».

    Lo que se pierde con `None` es el desglose, no la respuesta: `amount_out`
    sigue siendo neto y comparable contra cualquier otro venue. Lo que no se
    puede hacer con una comisión desconocida es **restarla**, y de eso se ocupa
    `app.usecases.scan_opportunities`, que descarta esas cotizaciones al
    calcular diferenciales netos.
    """

    venue: Venue
    pair: TradingPair
    amount_in: TokenAmount
    amount_out: TokenAmount
    #: Motor que observó esta cotización (`EngineManifest.engine_id`).
    #:
    #: Va en la cotización y no en el `Venue` a propósito. El `Venue` identifica
    #: **dónde** se opera —el pool o el agregado— y es deliberadamente igual lo
    #: mire quien lo mire, para que dos motores que observan el mismo pool no
    #: parezcan dos sitios distintos. Lo que sí es del motor es **cómo se
    #: construye** el payload: dos motores pueden cotizar el mismo par contra el
    #: mismo pool y producir transacciones completamente distintas.
    #:
    #: Se necesita porque construir con un motor distinto del que cotizó sería
    #: cambiar el venue a espaldas del usuario: la cifra que vio y el payload que
    #: va a firmar tienen que salir del mismo sitio. Ver
    #: `app.usecases.prepare_swap`.
    engine_id: str
    #: Comisión del venue, o `None` si la fuente no la separa de `amount_out`.
    fee_bps: BasisPoints | None
    #: Impacto de precio, o `None` si la fuente **no lo publica**.
    #:
    #: Mismo razonamiento que en `fee_bps`, y por el mismo camino: tres formas
    #: de contarlo —publicar 0, estimarlo, o decir que no se sabe— y las dos
    #: primeras descartadas. El caso medido es la API de 0x, que **eliminó** el
    #: campo en su versión 2: tenía `estimatedPriceImpact` en la v1 y ya no lo
    #: devuelve, junto con el parámetro que lo limitaba. Lo dice su propia
    #: documentación, y no es un fallo de la respuesta sino una decisión suya.
    #:
    #: Estimarlo está descartado por una razón concreta y medida, no por
    #: prudencia: la recomendación habitual —comparar el valor de lo entregado
    #: con el de lo recibido— **no mide el impacto**. 0x avisa de que esa resta
    #: confunde el excedente por slippage positivo con impacto, y hay una trampa
    #: peor que se comprobó aquí: calcularlo contra una orden diminuta del mismo
    #: par da un 0,93 % de «impacto» en una orden de 1 WETH, que no mueve el
    #: precio de nadie. La causa es que a ese tamaño el agregador enruta por
    #: Curve y a tamaño normal por Uniswap V3, así que la diferencia es de ruta
    #: y no de profundidad. Publicar ese número habría pintado un 100 bps falso
    #: en cada cotización.
    #:
    #: `None` significa exactamente «la fuente no lo dice». Lo que no se pierde
    #: es la comparación: `amount_out` ya viene neto de impacto, así que sigue
    #: siendo comparable contra cualquier otro venue.
    price_impact_bps: BasisPoints | None
    observed_at: datetime
    liquidity: TokenAmount | None = None
    #: Procedencia de `fee_bps`, y `None` cuando `fee_bps` lo es. Por defecto lo
    #: más honesto que puede afirmar un motor genérico; cada motor real lo ajusta
    #: a lo que de verdad publica su fuente.
    fee_basis: Measurement | None = Measurement.REPORTED
    #: Procedencia de `price_impact_bps`, y `None` cuando no hay impacto que
    #: procede de ningún sitio. La coherencia entre los dos la exige
    #: `__post_init__`, igual que con la comisión.
    impact_basis: Measurement | None = Measurement.DERIVED
    #: Nota del motor sobre cómo se obtuvo la cifra, para mostrar junto al dato.
    source_note: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        # Una cotización sin motor no se puede construir: nadie sabría a quién
        # pedirle el payload. Se corta al crearla, que es cuando se sabe.
        if not self.engine_id:
            raise InvalidAmountError(
                "la cotización necesita el `engine_id` del motor que la observó: "
                "sin él no se puede saber qué motor debe construir el swap"
            )
        # Los dos campos describen la misma cifra, así que o están los dos o no
        # está ninguno. Permitir «comisión desconocida con procedencia
        # publicada» dejaría pasar un estado que no quiere decir nada.
        if (self.fee_bps is None) != (self.fee_basis is None):
            raise InvalidAmountError(
                f"fee_bps={self.fee_bps!r} y fee_basis={self.fee_basis!r} no son "
                f"coherentes: una comisión desconocida no tiene procedencia, y "
                f"una conocida la necesita"
            )
        # La misma regla para el impacto, ahora que también puede faltar.
        if (self.price_impact_bps is None) != (self.impact_basis is None):
            raise InvalidAmountError(
                f"price_impact_bps={self.price_impact_bps!r} e "
                f"impact_basis={self.impact_basis!r} no son coherentes: un "
                f"impacto desconocido no tiene procedencia, y uno conocido la "
                f"necesita"
            )
        if self.amount_in.symbol != self.pair.base.symbol:
            raise CurrencyMismatchError(
                f"amount_in es {self.amount_in.symbol} pero la base del par "
                f"{self.pair.symbol} es {self.pair.base.symbol}"
            )
        if self.amount_out.symbol != self.pair.quote.symbol:
            raise CurrencyMismatchError(
                f"amount_out es {self.amount_out.symbol} pero el quote del par "
                f"{self.pair.symbol} es {self.pair.quote.symbol}"
            )
        if not self.amount_in.is_positive:
            raise InvalidAmountError("amount_in debe ser positivo")
        if self.amount_out.raw < 0:
            raise InvalidAmountError("amount_out no puede ser negativo")

    @property
    def fee_is_known(self) -> bool:
        """Si la fuente desglosa la comisión, aparte de cobrarla en `amount_out`."""
        return self.fee_bps is not None

    @property
    def is_exact(self) -> bool:
        """Si ninguna de las cifras de la cotización es una estimación.

        Una comisión **desconocida** no la hace inexacta: `amount_out` sigue
        siendo la cantidad medida que se recibiría, y comparar por `amount_out`
        sigue siendo válido. Lo que falta es el desglose, y eso lo dice
        `fee_is_known`. Mezclar las dos cosas en una sola bandera haría que la
        UI marcara como «aproximada» una cifra que está medida al dígito.

        Lo mismo vale para un impacto desconocido, y por la misma razón: falta
        una cifra que describe lo que ya está dentro de `amount_out`, no la
        cifra con la que se compara.
        """
        return (self.fee_basis is None or self.fee_basis.is_exact) and (
            self.impact_basis is None or self.impact_basis.is_exact
        )

    @property
    def impact_is_known(self) -> bool:
        """Si la fuente publica el impacto, aparte de aplicarlo.

        Simétrico de `fee_is_known`: quien necesite la cifra —para avisar de un
        impacto alto, por ejemplo— tiene que preguntar, en vez de leer un `None`
        como si fuera un cero.
        """
        return self.price_impact_bps is not None

    @property
    def price(self) -> Price:
        """Precio efectivo de ejecución, neto de comisiones e impacto."""
        return self.amount_in.price_against(self.amount_out)

    def __str__(self) -> str:
        return f"{self.venue.name}: {self.amount_in} → {self.amount_out} ({self.price})"


# --------------------------------------------------------------------------- #
# Puentes entre redes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BridgeRequest:
    """Mover un token de una red a otra, con el mismo token o con otro distinto.

    ### Por qué esto **no** es un `TradingPair`

    `TradingPair.__post_init__` rechaza que las dos patas estén en redes
    distintas, y hace bien: un par es una pregunta sobre un mercado, y un mercado
    vive en una sola red. Un puente es lo contrario —su razón de ser es cruzar—,
    así que expresarlo como par obligaría a relajar la invariante que protege todo
    lo demás. De ahí un modelo propio en vez de un `Quote`.

    Las dos patas llevan su red cada una, y por eso no hace falta un campo
    `from_chain`/`to_chain` aparte: el token ya la lleva, y dos sitios donde
    decir lo mismo es un sitio donde pueden discrepar.
    """

    origin: Token
    destination: Token
    amount_in: TokenAmount

    def __post_init__(self) -> None:
        if self.origin.chain == self.destination.chain:
            raise CurrencyMismatchError(
                f"las dos patas de un puente están en «{self.origin.chain}»: para "
                f"cambiar dos tokens de la misma red se usa un swap, no un puente. "
                f"Un puente que no cruza no tiene ruta que buscar."
            )
        if self.amount_in.raw <= 0:
            raise InvalidAmountError(
                f"el importe a mover debe ser positivo, llegó {self.amount_in}"
            )
        # El importe tiene que ser del token de origen: si no, se estaría pidiendo
        # mover una cantidad de un token distinto al que se entrega, y la cifra
        # que se compara contra los topes no sería la que sale de la cartera.
        if (
            self.amount_in.symbol != self.origin.symbol
            or self.amount_in.decimals != self.origin.decimals
        ):
            raise CurrencyMismatchError(
                f"el importe {self.amount_in} no es del token de origen "
                f"({self.origin.symbol}): no se puede mover una cantidad que no es "
                f"la que se entrega."
            )

    @property
    def symbol(self) -> str:
        """Cómo se nombra este puente: `USDC@base → USDC@polygon`."""
        return (
            f"{self.origin.symbol}@{self.origin.chain} → "
            f"{self.destination.symbol}@{self.destination.chain}"
        )

    @property
    def chains(self) -> tuple[str, str]:
        """Las dos redes que toca, origen primero."""
        return (self.origin.chain, self.destination.chain)


@dataclass(frozen=True, slots=True)
class BridgeQuote:
    """Una ruta concreta para un `BridgeRequest`, con lo que cuesta y lo que tarda.

    ### Lo que se ordena

    `amount_out` es la cifra con la que se comparan dos rutas, y por eso es la
    única que **tiene** que existir. Las dos patas del destino son el mismo token
    —la petición lo fija—, así que dos importes de salida son directamente
    comparables y la mejor ruta es la que más entrega.

    `amount_out_min` es el suelo garantizado, y no es un adorno: en un puente la
    diferencia entre lo estimado y lo mínimo es lo que absorbe el movimiento del
    mercado mientras la transacción viaja, y un puente tarda minutos, no bloques.
    Enseñar sólo la estimación sería prometer una cifra que nadie garantiza.

    ### Lo que puede faltar, y por qué se dice

    `fee` es `None` cuando la fuente no desglosa la comisión —el mismo criterio
    que `Quote.fee_bps`: una comisión publicada de cero no se acepta nunca, y una
    inventada es peor que un hueco—. `duration_seconds` es `None` cuando la
    fuente no lo publica. Ninguno de los dos impide comparar, porque la
    comparación la decide `amount_out`.
    """

    engine_id: str
    #: El puente que de verdad se usó por debajo (Across, Stargate, Relay…). Un
    #: agregado reparte entre varios, y saber cuál eligió es la mitad de entender
    #: por qué el precio es el que es.
    provider: str
    request: BridgeRequest
    amount_out: TokenAmount
    amount_out_min: TokenAmount
    fee: TokenAmount | None = None
    fee_basis: Measurement | None = None
    duration_seconds: int | None = None
    observed_at: datetime | None = None
    #: Por dónde pasa la ruta, en texto, tal como lo publica la fuente.
    route: str = ""
    source_note: str = ""

    def __post_init__(self) -> None:
        if not self.engine_id:
            raise InvalidAmountError("una ruta de puente necesita el motor que la produjo")
        if self.amount_out.raw <= 0:
            raise InvalidAmountError(
                f"una ruta de puente sin importe de salida no describe nada, llegó "
                f"{self.amount_out}"
            )
        if self.amount_out_min.raw < 0 or self.amount_out_min.raw > self.amount_out.raw:
            # Un mínimo por encima del estimado es una respuesta que se
            # contradice: no se puede garantizar más de lo que se espera recibir.
            raise InvalidAmountError(
                f"el mínimo garantizado ({self.amount_out_min}) no puede superar lo "
                f"estimado ({self.amount_out})"
            )
        if self.amount_out.decimals != self.request.destination.decimals:
            raise CurrencyMismatchError(
                f"el importe de salida {self.amount_out} no está en la escala del "
                f"token de destino ({self.request.destination.symbol}, "
                f"{self.request.destination.decimals} decimales): compararlo con otra "
                f"ruta daría una cifra falsa."
            )
        for campo, importe in (
            ("amount_out", self.amount_out),
            ("amount_out_min", self.amount_out_min),
        ):
            if importe.symbol != self.request.destination.symbol:
                raise CurrencyMismatchError(
                    f"{campo} es {importe.symbol} pero la pata de destino del puente "
                    f"{self.pair_label} es {self.request.destination.symbol}"
                )
        # Los dos van juntos o no va ninguno, igual que en `Quote`: una comisión
        # sin decir de dónde salió no describe nada.
        if (self.fee is None) != (self.fee_basis is None):
            raise InvalidAmountError(
                "la comisión y su procedencia van juntas: una comisión sin decir "
                "cómo se obtuvo es una cifra que nadie puede comprobar."
            )

    @property
    def pair_label(self) -> str:
        return self.request.symbol

    @property
    def fee_bps(self) -> BasisPoints | None:
        """La comisión sobre lo **entregado**, o `None` si no se desglosa.

        Sobre lo entregado y no sobre lo recibido porque es lo que sale de la
        cartera, que es lo que el usuario reconoce. Mezclar bases —una ruta que
        la publica sobre la entrada y otra sobre la salida— haría que la
        comparación de comisiones significara dos cosas distintas.
        """
        if self.fee is None:
            return None
        delivered = self.request.amount_in
        if self.fee.symbol != delivered.symbol or self.fee.decimals != delivered.decimals:
            return None
        base = delivered.as_decimal()
        if base <= 0:
            return None
        return BasisPoints.from_ratio(EXACT.divide(self.fee.as_decimal(), base))

    @property
    def duration_label(self) -> str:
        """La duración en palabras, o que no se sabe."""
        if self.duration_seconds is None:
            return "sin dato"
        seconds = self.duration_seconds
        if seconds < 60:
            return f"{seconds} s"
        if seconds < 3_600:
            return f"{seconds // 60} min"
        return f"{seconds / 3_600:.1f} h"

    @property
    def is_cross_chain(self) -> bool:
        """Siempre sí, por construcción: lo garantiza `BridgeRequest`."""
        return self.request.origin.chain != self.request.destination.chain

    def __str__(self) -> str:
        return (
            f"{self.provider} ({self.engine_id}): {self.request.amount_in} → "
            f"{self.amount_out} en {self.pair_label}"
        )


@dataclass(frozen=True, slots=True)
class BridgeRoute:
    """Una ruta de puente, con su cotización y quién la firma.

    No añade nada que no esté en `BridgeQuote` salvo el orden, y el orden existe
    para que la interfaz no lo recalcule: si la pantalla ordenara por su cuenta y
    el caso de uso por la suya, la ruta que se enseña como mejor podría no ser la
    que se ejecuta. Se ordena **una vez**, aquí.
    """

    quote: BridgeQuote
    #: Posición en la lista ordenada, empezando en 1. Es lo que se enseña.
    position: int

    @property
    def is_best(self) -> bool:
        return self.position == 1


def rank_bridges(quotes: Sequence[BridgeQuote]) -> tuple[BridgeRoute, ...]:
    """Ordena las rutas de mejor a peor por lo que entregan.

    El desempate importa y es deliberado: a igualdad de importe recibido gana el
    que **garantiza** más (`amount_out_min`), y si también empatan, el que tarda
    menos. Nunca se desempata por el orden en que llegaron las respuestas, porque
    eso haría que la mejor ruta dependiera de qué servidor contestó antes — un
    dato que no dice nada sobre el precio y que cambiaría de una consulta a la
    siguiente.

    Y no se descarta ninguna por tener peor precio: enseñar sólo la mejor
    escondería que la segunda está a un 0,1 %, que es justo lo que permite
    decidir si merece la pena esperar a que la primera se recupere.
    """
    ordered = sorted(
        quotes,
        key=lambda q: (
            -q.amount_out.raw,
            -q.amount_out_min.raw,
            q.duration_seconds if q.duration_seconds is not None else 2**31,
            q.engine_id,
        ),
    )
    return tuple(BridgeRoute(quote=quote, position=i) for i, quote in enumerate(ordered, start=1))


@dataclass(frozen=True, slots=True)
class PriceComparison:
    """Las cotizaciones de un mismo `amount_in` en varios venues.

    `quotes` puede traer cifras de **varios motores a la vez**: la ranura de
    cotización guarda una pila, y comparar es precisamente el momento en que
    todas aportan. Por eso cada `Quote` lleva su `engine_id`, y por eso existe
    `failed_engines`: cuando una fuente no responde, la comparación sigue con
    las demás —no detenerse es un requisito, no una comodidad— pero lo dice.
    Una tabla con cuatro filas donde había cinco fuentes no puede parecer
    completa.
    """

    pair: TradingPair
    amount_in: TokenAmount
    quotes: tuple[Quote, ...]
    #: Ids de los motores que se consultaron y no respondieron. Va aquí y no en
    #: un log porque el usuario es quien tiene que saber que la comparación está
    #: incompleta, y para entonces el log ya no está en pantalla.
    failed_engines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.quotes:
            raise InvalidAmountError("una comparación necesita al menos una cotización")
        for quote in self.quotes:
            if quote.pair != self.pair:
                raise CurrencyMismatchError(
                    f"la cotización de {quote.venue.name} es de {quote.pair.symbol}, "
                    f"no de {self.pair.symbol}"
                )
            if quote.amount_in != self.amount_in:
                raise CurrencyMismatchError(
                    f"la cotización de {quote.venue.name} es por {quote.amount_in}, "
                    f"no por {self.amount_in}"
                )

    @property
    def ranked(self) -> tuple[Quote, ...]:
        """De mejor a peor para quien vende: más `amount_out` primero."""
        return tuple(sorted(self.quotes, key=lambda q: q.amount_out.raw, reverse=True))

    @property
    def best(self) -> Quote:
        return self.ranked[0]

    @property
    def worst(self) -> Quote:
        return self.ranked[-1]

    @property
    def spread_bps(self) -> BasisPoints:
        """Dispersión entre la mejor y la peor ejecución disponible."""
        if self.worst.amount_out.is_zero:
            return BasisPoints(0)
        return self.best.price.difference_from(self.worst.price)

    @property
    def has_estimates(self) -> bool:
        """Si alguna cotización incluye una cifra estimada, no medida.

        La UI lo usa para avisar: un diferencial entre una cotización medida y
        otra estimada no se puede leer igual que uno entre dos medidas.
        """
        return any(not quote.is_exact for quote in self.quotes)

    @property
    def has_unknown_fees(self) -> bool:
        """Si alguna cotización no desglosa su comisión.

        Distinto de `has_estimates`: aquí las cifras que hay están medidas, pero
        falta una. Afecta a lo que se puede *restar*, no a lo que se puede
        comparar, y la UI lo marca aparte por eso.
        """
        return any(not quote.fee_is_known for quote in self.quotes)

    @property
    def has_partial_sources(self) -> bool:
        """Si alguna fuente consultada no llegó a responder.

        Se separa de `has_estimates` y de `has_unknown_fees` porque es un tercer
        modo de estar incompleta, y el más fácil de confundir con «no hay
        oportunidad»: si la fuente que habría dado el mejor precio es justo la
        que se cayó, la comparación se ve perfectamente normal y está mintiendo
        por omisión. Ver `failed_engines`.
        """
        return bool(self.failed_engines)

    @property
    def observed_at(self) -> datetime:
        """La observación más antigua de la comparación.

        Se toma la más antigua a propósito: la frescura de una comparación es la
        de su dato más viejo, no la del más reciente.
        """
        return min(quote.observed_at for quote in self.quotes)


@dataclass(frozen=True, slots=True)
class BridgeComparison:
    """Las rutas que devolvieron todos los motores de puentes activos, ordenadas.

    Guarda `BridgeRoute` y no `BridgeQuote` porque el orden se decide **una vez**
    —en `rank_bridges`, al construir esto— y la interfaz sólo lo lee. Si la
    pantalla ordenara por su cuenta, la ruta que se enseña como mejor podría no
    ser la que se ejecuta cuando alguien pulse el botón, y esa diferencia se paga
    con dinero.

    `failed_engines` existe por la misma razón que en `PriceComparison`: una tabla
    con las rutas de dos motores donde había tres no puede parecer completa. Aquí
    pesa más todavía, porque el motor que no respondió puede ser justo el que
    cruzaba en menos tiempo o con menos comisión.
    """

    request: BridgeRequest
    routes: tuple[BridgeRoute, ...]
    failed_engines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.routes:
            raise InvalidAmountError("una comparación de puentes necesita una ruta")
        for position, route in enumerate(self.routes, start=1):
            # El orden viene impuesto y la posición se enseña, así que una lista
            # desordenada —o con posiciones repetidas— no sería un detalle de
            # presentación: sería una tabla que numera mal la mejor ruta.
            if route.position != position:
                raise InvalidAmountError(
                    f"las rutas de un puente vienen ordenadas y numeradas: la "
                    f"{position}ª trae la posición {route.position}."
                )
            if route.quote.request != self.request:
                raise CurrencyMismatchError(
                    f"la ruta de {route.quote.provider} cruza "
                    f"{route.quote.pair_label}, no {self.request.symbol}"
                )

    @property
    def best(self) -> BridgeRoute:
        """La mejor ruta: la primera, que es la que se ofrece ejecutar."""
        return self.routes[0]

    @property
    def has_unknown_fees(self) -> bool:
        """Si alguna ruta no desglosa su comisión.

        Distinto de que sea cara: aquí lo que falta es el dato. Se enseña aparte
        porque una ruta sin comisión declarada parece gratis, y elegir la que
        parece gratis es exactamente el error que este aviso evita.

        Se mira `fee_bps` y no `fee`: un importe de comisión en una moneda que no
        es la entregada tampoco se puede comparar con los demás, así que cuenta
        como no desglosada — que es justo lo que `fee_bps` devuelve en ese caso.
        """
        return any(route.quote.fee_bps is None for route in self.routes)

    @property
    def has_partial_sources(self) -> bool:
        """Si algún motor consultado no llegó a responder."""
        return bool(self.failed_engines)

    @property
    def observed_at(self) -> datetime:
        """La observación más antigua: la frescura es la del dato más viejo.

        Exige que **todas** las rutas traigan su hora. Si a una le falta, la edad
        del conjunto no se sabe —y devolver la más antigua de las que sí la traen
        afirmaría una frescura sobre una ruta de la que no consta nada—. Los dos
        motores de puentes la publican siempre, así que esto no es un caso que
        ocurra: es lo que impide que ocurra sin que nadie se entere.
        """
        moments = [route.quote.observed_at for route in self.routes]
        if any(moment is None for moment in moments):
            raise InvalidAmountError(
                "no se puede fechar la comparación: alguna ruta no declara cuándo "
                "se observó, y la frescura de una comparación es la de su dato más "
                "viejo."
            )
        return min(moment for moment in moments if moment is not None)


@dataclass(frozen=True, slots=True)
class Opportunity:
    """Discrepancia observada entre dos venues para el mismo tamaño de orden.

    Es un **hallazgo analítico**, no una instrucción: el producto informa de la
    diferencia y de lo que se la come en comisiones. Decidir es del usuario.
    """

    pair: TradingPair
    amount_in: TokenAmount
    best: Quote
    reference: Quote
    total_fee_bps: BasisPoints
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.best.venue.venue_id == self.reference.venue.venue_id:
            raise InvalidAmountError("una oportunidad necesita dos venues distintos")

    @property
    def gross_spread_bps(self) -> BasisPoints:
        return self.best.price.difference_from(self.reference.price)

    @property
    def net_spread_bps(self) -> BasisPoints:
        """Diferencial que queda tras descontar las comisiones de ambos lados."""
        return BasisPoints(self.gross_spread_bps.value - self.total_fee_bps.value)

    @property
    def is_actionable(self) -> bool:
        """Si el diferencial sobrevive a las comisiones. No incluye gas."""
        return self.net_spread_bps.value > 0


# --------------------------------------------------------------------------- #
# Mercados de predicción
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MarketOutcome:
    """Un resultado posible y su precio en [0, 1], que es su probabilidad.

    `token_id` es el nombre **operable** del resultado: en Polymarket es el
    `tokenId` del ERC-1155 condicional, el número que hay que firmar dentro de
    una orden. Es `None` en un mercado que sólo se leyó para mirarlo, y eso no lo
    invalida: el mercado sigue sirviendo para calcular probabilidades y deja de
    servir para operar. Quien quiera distinguirlo tiene `is_tradeable`.
    """

    label: str
    price: Decimal
    token_id: str | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise InvalidAmountError("el resultado necesita una etiqueta")
        if not self.price.is_finite() or not (Decimal(0) <= self.price <= Decimal(1)):
            raise InvalidAmountError(
                f"el precio de «{self.label}» debe estar en [0, 1], llegó {self.price}"
            )
        if self.token_id is not None and not self.token_id.isdigit():
            # Se exige que sea una cadena de dígitos y no cualquier texto: el
            # `tokenId` de un ERC-1155 es un `uint256`, y en JSON viaja como
            # cadena precisamente porque no cabe en un número de coma flotante
            # sin perder dígitos. Aceptar aquí algo que no sea eso trasladaría el
            # fallo a la firma, que es donde ya cuesta dinero.
            raise InvalidAmountError(
                f"el identificador del resultado «{self.label}» debe ser el "
                f"uint256 en decimal, llegó {self.token_id!r}"
            )

    @property
    def is_tradeable(self) -> bool:
        """Si este resultado se puede nombrar dentro de una orden."""
        return self.token_id is not None

    @property
    def implied_probability(self) -> Decimal:
        return self.price

    @property
    def implied_percent(self) -> Decimal:
        return EXACT.multiply(self.price, Decimal(100))


@dataclass(frozen=True, slots=True)
class PredictionMarket:
    """Un mercado de predicción con sus resultados mutuamente excluyentes.

    Los cuatro campos que siguen a `closes_at` son los que convierten un mercado
    **legible** en uno **operable**, y todos valen `None` por omisión para que un
    mercado leído sin ellos siga siendo un mercado válido. Lo que no se puede es
    firmar una orden contra un mercado del que no constan: `is_tradeable` lo dice
    y `tradeability_blockers` explica cuál falta.
    """

    market_id: str
    venue: Venue
    question: str
    outcomes: tuple[MarketOutcome, ...]
    observed_at: datetime
    closes_at: datetime | None = None
    #: Identificador del mercado en la cadena: en Polymarket, el `conditionId`
    #: del contrato de tokens condicionales. Es el «mercado» que el CLOB pide al
    #: publicar una orden, distinto del `market_id` con el que la fuente lo
    #: nombra en sus listados.
    condition_id: str | None = None
    #: Si el mercado pertenece a un conjunto de resultados excluyentes que
    #: comparten colateral. No es cosmético: decide **contra qué contrato** se
    #: firma la orden, porque Polymarket tiene un contrato de intercambio general
    #: y otro para estos mercados.
    #:
    #: Es `bool | None` y no `bool` a propósito: `False` afirma «este mercado no
    #: es de ese tipo», y esa es una afirmación que sólo se puede hacer cuando la
    #: fuente lo ha dicho. Con `None` no se puede firmar, que es lo correcto.
    neg_risk: bool | None = None
    #: Salto mínimo de precio admitido. Fuera de él la orden se rechaza, así que
    #: es la tabla con la que hay que redondear antes de firmar.
    tick_size: Decimal | None = None
    #: Participaciones mínimas por orden. Medido: en Polymarket son 5, y no es un
    #: detalle de la interfaz —una orden por debajo se rechaza entera—.
    min_order_size: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.closes_at is not None:
            _require_aware(self.closes_at, "closes_at")
        if len(self.outcomes) < 2:
            raise InvalidAmountError(
                f"el mercado «{self.question}» necesita al menos dos resultados"
            )
        labels = [outcome.label for outcome in self.outcomes]
        if len(set(labels)) != len(labels):
            raise InvalidAmountError(f"resultados duplicados en «{self.question}»")
        if self.tick_size is not None and not (
            self.tick_size.is_finite() and Decimal(0) < self.tick_size < Decimal(1)
        ):
            raise InvalidAmountError(
                f"el salto de precio de «{self.question}» debe estar en (0, 1), "
                f"llegó {self.tick_size}"
            )
        if self.min_order_size is not None and not (
            self.min_order_size.is_finite() and self.min_order_size > 0
        ):
            raise InvalidAmountError(
                f"el mínimo por orden de «{self.question}» debe ser positivo, "
                f"llegó {self.min_order_size}"
            )

    @property
    def total_implied_probability(self) -> Decimal:
        total = Decimal(0)
        for outcome in self.outcomes:
            total = EXACT.add(total, outcome.price)
        return total

    @property
    def overround_bps(self) -> BasisPoints:
        """Cuánto se desvía de 1 la suma de probabilidades.

        Positivo = el mercado cobra margen (suma > 100 %). Negativo = las
        probabilidades no llegan a 1, que es la discrepancia interesante.
        """
        return BasisPoints.from_ratio(EXACT.subtract(self.total_implied_probability, Decimal(1)))

    def is_coherent(self, tolerance: BasisPoints = DEFAULT_OVERROUND_TOLERANCE) -> bool:
        return abs(self.overround_bps).value <= tolerance.value

    @property
    def favourite(self) -> MarketOutcome:
        return max(self.outcomes, key=lambda outcome: outcome.price)

    def time_left(self, now: datetime) -> timedelta | None:
        """Cuánto falta para que cierre, o `None` si no se sabe o ya cerró.

        Un mercado sin fecha de cierre y uno ya cerrado se responden igual
        —`None`— porque para quien pregunta «cuánto queda» las dos cosas son lo
        mismo: no hay tiempo por delante. Distinguirlos es trabajo de quien tenga
        el dato de si el mercado está abierto, no de esta cuenta.

        El cierre en el instante exacto cuenta como cerrado: un mercado que
        termina justo ahora ya no admite una orden, y devolver un `timedelta` de
        cero invitaría a operarlo.
        """
        _require_aware(now, "now")
        if self.closes_at is None or self.closes_at <= now:
            return None
        return self.closes_at - now

    def outcome(self, label: str) -> MarketOutcome | None:
        """El resultado con esa etiqueta, o `None`. La comparación es exacta.

        Exacta y no «que contenga»: «Up» está contenido en «Up or Down», y
        elegir el resultado equivocado por una coincidencia parcial es la forma
        más silenciosa de comprar lo contrario de lo que se quería.
        """
        return next((item for item in self.outcomes if item.label == label), None)

    @property
    def is_tradeable(self) -> bool:
        """Si se puede construir y firmar una orden contra este mercado."""
        return not self.tradeability_blockers()

    def tradeability_blockers(self) -> tuple[str, ...]:
        """Por qué **no** se puede operar, en castellano y en orden de lectura.

        Devuelve una lista y no un booleano porque la interfaz tiene que poder
        apagar el botón **diciendo el motivo**: «no se puede» sin decir qué falta
        obliga al usuario a adivinar si el problema es su saldo, su configuración
        o el mercado. Un mercado leído sin estos datos sigue siendo un mercado
        perfectamente válido para mirar; lo único que no es es operable.
        """
        faltas: list[str] = []
        if self.condition_id is None:
            faltas.append("la fuente no publicó el identificador del mercado")
        if self.neg_risk is None:
            faltas.append("no consta si el mercado comparte colateral con otros")
        if self.tick_size is None:
            faltas.append("no consta el salto mínimo de precio")
        if self.min_order_size is None:
            faltas.append("no consta el mínimo de participaciones por orden")
        without_id = [o.label for o in self.outcomes if o.token_id is None]
        if without_id:
            faltas.append(
                "la fuente no publicó el identificador de "
                + ("los resultados " if len(without_id) > 1 else "del resultado ")
                + ", ".join(without_id)
            )
        return tuple(faltas)


# --------------------------------------------------------------------------- #
# Posiciones: lo que ya se tiene
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PredictionPosition:
    """Participaciones que una cartera tiene en un resultado, según el recinto.

    Es la mitad que faltaba entre «mercado» y «cartera». Un mercado dice lo que
    **se puede** comprar; una posición dice lo que **se tiene**, y sólo lo
    segundo se puede cobrar. Se lee del recinto por cartera y no se deduce de las
    órdenes publicadas: entre publicar y tener hay un cruce que ocurre en el
    libro de otro, así que la única fuente de «cuánto tengo» es quien lo custodía.

    **`redeemable` es del recinto, no una cuenta nuestra.** Que un mercado haya
    resuelto y que la posición se pueda cobrar ya son lo mismo en el dato que
    publica Polymarket, y recalcularlo aquí a partir de la fecha de cierre sería
    inventar una segunda verdad que puede discrepar de la primera —el mercado
    puede resolver antes o después de la fecha, y una posición perdedora no se
    cobra nunca—. Se copia el hecho y se enseña.

    `shares` es el número de participaciones, y cada una paga 1 unidad de
    colateral si acierta. Por eso `payout` es `shares` y no `shares x precio`: al
    cobrar no se vende a un precio, se cambia la participación por su valor
    nominal. El precio actual (`cur_price`) sólo sirve para valorar lo que
    todavía **no** se puede cobrar.
    """

    venue: Venue
    #: El `conditionId` del contrato de tokens condicionales. Es a la vez el
    #: mercado y la clave con la que se cobra: `redeemPositions` lo pide.
    condition_id: str
    question: str
    outcome_label: str
    #: El `tokenId` del ERC-1155, que identifica el resultado dentro del mercado.
    token_id: str
    shares: Decimal
    observed_at: datetime
    redeemable: bool = False
    #: Si el mercado comparte colateral con otros. Decide **contra qué contrato**
    #: se cobra, y cobrar contra el que no es revierte. `None` significa que el
    #: recinto no lo dijo, y con `None` no se construye nada.
    neg_risk: bool | None = None
    #: Precio actual de la participación, para poder valorar lo que aún no se
    #: puede cobrar. Puede faltar: no todas las fuentes lo publican.
    cur_price: Decimal | None = None
    #: Precio medio de entrada de las participaciones que se tienen. Puede
    #: faltar: no todas las fuentes lo publican, y sin él no se puede decir
    #: **cuánto se ha ganado** frente a lo pagado —sólo cuánto vale hoy—.
    avg_price: Decimal | None = None
    #: Resultado no realizado de la posición, en unidades de colateral, según la
    #: fuente. El signo es de la fuente: un negativo es una pérdida latente.
    cash_pnl: Decimal | None = None
    #: El mismo resultado en porcentaje sobre lo desembolsado, según la fuente.
    percent_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if not self.condition_id:
            raise InvalidAmountError(
                f"la posición en «{self.question}» llegó sin el identificador del "
                f"mercado, y sin él no se puede cobrar"
            )
        if not self.token_id:
            raise InvalidAmountError(
                f"la posición en «{self.question}» llegó sin el identificador del "
                f"resultado"
            )
        if self.shares <= 0:
            raise InvalidAmountError(
                f"una posición de cero participaciones no es una posición, llegó "
                f"{self.shares} en «{self.question}»"
            )
        if self.cur_price is not None and not (
            Decimal(0) <= self.cur_price <= Decimal(1)
        ):
            raise InvalidAmountError(
                f"el precio de una participación está en [0, 1], llegó "
                f"{self.cur_price} en «{self.question}»"
            )
        if self.avg_price is not None and not (
            Decimal(0) <= self.avg_price <= Decimal(1)
        ):
            raise InvalidAmountError(
                f"el precio medio de entrada está en [0, 1], llegó "
                f"{self.avg_price} en «{self.question}»"
            )

    @property
    def payout(self) -> Decimal:
        """Lo que se cobra al redimir: una participación ganadora paga 1.

        Sin restar comisión, porque redimir no la tiene: la comisión ya se pagó
        al entrar. Lo que devuelve esto es el importe **bruto** que el contrato
        transferirá, que es exactamente lo que hay que enseñar antes de firmar.
        """
        return self.shares

    @property
    def redeemability_blockers(self) -> tuple[str, ...]:
        """Por qué **no** se puede cobrar esta posición, en orden de lectura.

        Gemelo de `PredictionMarket.tradeability_blockers`, y por la misma razón:
        un botón apagado sin motivo se lee como «esto no funciona» y empuja a
        buscar la forma de saltárselo. Aquí, además, el motivo más común —«este
        mercado todavía no ha resuelto»— no es un fallo de nadie, y decirlo así
        evita que parezca uno.
        """
        faltas: list[str] = []
        if not self.redeemable:
            faltas.append(
                "el mercado todavía no ha resuelto, así que estas participaciones "
                "todavía no se pueden cambiar por colateral"
            )
        if self.neg_risk is None:
            faltas.append(
                "no consta si el mercado comparte colateral con otros, y eso "
                "decide contra qué contrato se cobra"
            )
        elif self.neg_risk:
            # Los mercados de resultados excluyentes no se cobran contra el
            # contrato condicional como los demás: sus participaciones pasan por
            # un adaptador que las convierte, y ese camino **no está medido** en
            # este programa. Se dice así y no se construye nada. Rellenar el
            # hueco con una dirección copiada de la documentación sería firmar
            # contra una suposición, y un cobro contra el contrato equivocado
            # quema gas y no devuelve nada — o peor, si el contrato tiene una
            # función con el mismo nombre y otra semántica.
            faltas.append(
                "este mercado comparte colateral con otros y se cobra por un "
                "adaptador distinto del contrato condicional; ese camino no está "
                "medido todavía, así que no se firma"
            )
        return tuple(faltas)

    @property
    def is_redeemable(self) -> bool:
        return not self.redeemability_blockers


# --------------------------------------------------------------------------- #
# Profundidad de libro
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DepthLevel:
    """Un precio del libro y cuántas participaciones hay a ese precio."""

    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.price < Decimal(1)):
            raise InvalidAmountError(
                f"un nivel de libro debe estar en (0, 1), llegó {self.price}"
            )
        if self.size <= 0:
            raise InvalidAmountError(
                f"un nivel de libro necesita tamaño positivo, llegó {self.size}"
            )

    @property
    def notional(self) -> Decimal:
        return EXACT.multiply(self.price, self.size)


@dataclass(frozen=True, slots=True)
class MarketDepth:
    """Lo que de verdad hay en el libro de un resultado, ordenado de mejor a peor.

    Existe porque un precio publicado es una **opinión** y el libro es un hecho.
    La probabilidad implícita de un resultado es el último precio cruzado; lo que
    costaría comprar cinco participaciones **ahora** es otra cosa, y para un
    tamaño dado la diferencia puede ser el margen entero de una operación.

    Los dos lados vienen ordenados de mejor a peor desde el punto de vista de
    quien **cruza**: la mejor compra (el `bid` más alto) primero, y la mejor
    venta (el `ask` más bajo) primero. Así, recorrer una lista para calcular el
    coste de un tamaño es recorrerla en el orden en que se consumiría.

    `bids` y `asks` pueden estar vacíos: un libro de un solo lado es normal en un
    mercado que acaba de abrir o que está a punto de cerrar.
    """

    token_id: str
    bids: tuple[DepthLevel, ...] = ()
    asks: tuple[DepthLevel, ...] = ()
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.token_id.isdigit():
            raise InvalidAmountError(
                f"el libro necesita el uint256 del resultado, llegó {self.token_id!r}"
            )
        for nombre, niveles, mejor_primero in (
            ("las compras", self.bids, True),
            ("las ventas", self.asks, False),
        ):
            precios = [nivel.price for nivel in niveles]
            esperado = sorted(precios, reverse=mejor_primero)
            if precios != esperado:
                # Se comprueba el orden porque de él depende todo lo que se
                # calcula después: un libro ordenado al revés produciría un coste
                # más caro que el real, y un cálculo que se equivoca siempre en
                # la misma dirección se acaba dando por bueno.
                raise InvalidAmountError(
                    f"{nombre} del libro no vienen de mejor a peor: {precios}"
                )

    @property
    def best_bid(self) -> Decimal | None:
        """Lo máximo que ofrecen por una participación. `None` si no hay compras."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        """Lo mínimo que cuesta una participación. `None` si no hay ventas."""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        """El hueco entre lo que piden y lo que ofrecen, o `None` sin los dos lados."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def available_to_buy(self) -> Decimal:
        """Cuántas participaciones se podrían comprar cruzando todo el libro."""
        return sum((nivel.size for nivel in self.asks), Decimal(0))

    @property
    def available_to_sell(self) -> Decimal:
        """Cuántas participaciones se podrían vender cruzando todo el libro."""
        return sum((nivel.size for nivel in self.bids), Decimal(0))

    def cost_to_buy(self, shares: Decimal) -> Decimal | None:
        """Lo que costaría comprar exactamente esas participaciones, o `None`.

        Devuelve `None` cuando el libro **no llega**: pedir un tamaño que el
        libro no tiene y devolver el coste de la parte que sí cabe daría un
        número con pinta de precio y sin serlo. Distinguir «no cabe» de «cuesta
        esto» es lo que permite decidir en vez de adivinar.
        """
        return _walk(self.asks, shares, consume=_cost)

    def proceeds_to_sell(self, shares: Decimal) -> Decimal | None:
        """Lo que darían por exactamente esas participaciones, o `None` si no llega."""
        return _walk(self.bids, shares, consume=_cost)

    def average_price_to_buy(self, shares: Decimal) -> Decimal | None:
        """El precio medio que resulta de cruzar el libro, o `None` si no llega."""
        cost = self.cost_to_buy(shares)
        return None if cost is None else EXACT.divide(cost, shares)

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Mejor compra", str(self.best_bid) if self.best_bid is not None else "sin compras"),
            ("Mejor venta", str(self.best_ask) if self.best_ask is not None else "sin ventas"),
            ("Hueco", str(self.spread) if self.spread is not None else "—"),
            ("Participaciones en venta", f"{self.available_to_buy:f}"),
            ("Participaciones en compra", f"{self.available_to_sell:f}"),
        )


def _cost(price: Decimal, size: Decimal) -> Decimal:
    return EXACT.multiply(price, size)


def _walk(
    levels: tuple[DepthLevel, ...],
    shares: Decimal,
    *,
    consume: Callable[[Decimal, Decimal], Decimal],
) -> Decimal | None:
    """Recorre los niveles gastando `shares` y suma lo que cuesta.

    `consume` decide qué se acumula —el coste en colateral— para que la misma
    marcha sirva a los dos lados: comprar gasta `ask x tamaño` y vender ingresa
    `bid x tamaño`, y la diferencia entre las dos es cuál de las dos listas se
    recorre, no cómo se suma.
    """
    if shares <= 0:
        return Decimal(0)
    restante = shares
    total = Decimal(0)
    for nivel in levels:
        if restante <= 0:
            break
        tomado = min(restante, nivel.size)
        total += consume(nivel.price, tomado)
        restante -= tomado
    if restante > 0:
        return None
    return total


# --------------------------------------------------------------------------- #
# Órdenes de mercado de predicción
# --------------------------------------------------------------------------- #
class PredictionSide(StrEnum):
    """De qué lado va una orden.

    Se llaman `BUY`/`SELL` y no «sí»/«no» porque describen la **operación**, no
    el resultado: se puede comprar «No» y eso es un `BUY`. Confundir las dos
    cosas es comprar lo contrario de lo que se quería.
    """

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class PredictionOrder:
    """Una orden de predicción construida y **todavía sin firmar**.

    Describe el intercambio completo: cuántas participaciones, a qué precio como
    mucho, y qué se paga o se cobra por ellas. `price` y `size` ya vienen
    redondeados por el motor a las reglas del mercado —el salto de precio y el
    mínimo de participaciones—, porque redondear es responsabilidad de quien
    conoce la tabla del recinto, y aquí se **comprueba** que se cumplen en vez de
    darlas por buenas: un precio fuera del salto hace que el recinto rechace la
    orden entera, y descubrirlo al firmar es descubrirlo tarde.

    El lado del colateral —lo que de verdad se mueve— es `cost`: en una compra
    son dólares que salen, en una venta son dólares que entran, y en los dos
    casos es `precio x participaciones`. Es el número que se mide contra los
    topes y el que el usuario necesita ver antes de firmar.
    """

    venue_id: str
    chain: str
    market_id: str
    condition_id: str
    question: str
    outcome_label: str
    token_id: str
    side: PredictionSide
    price: Decimal
    size: Decimal
    tick_size: Decimal
    neg_risk: bool
    #: Tipo de orden del recinto. `GTC` —válida hasta que se cancele— es la
    #: única que se construye: las de ejecución inmediata no dejan nada que el
    #: usuario pueda revisar después, y esta entrega existe para poder revisar.
    order_type: str = "GTC"
    fee_rate_bps: int = 0
    nonce: int = 0
    expiration: int = 0

    def __post_init__(self) -> None:
        if not self.token_id.isdigit():
            raise InvalidAmountError(
                f"el identificador del resultado en «{self.question}» debe ser el "
                f"uint256 en decimal, llegó {self.token_id!r}"
            )
        if not self.tick_size.is_finite() or self.tick_size <= 0:
            raise InvalidAmountError(
                f"el salto de precio debe ser positivo, llegó {self.tick_size}"
            )
        if not (self.tick_size <= self.price <= Decimal(1) - self.tick_size):
            # El recinto no admite precios pegados a 0 ni a 1: un precio de 0 es
            # una orden que nadie puede cruzar y uno de 1 deja al comprador sin
            # margen. El límite es el propio salto, que es lo que publica la
            # fuente como rango válido.
            raise InvalidAmountError(
                f"el precio {self.price} queda fuera del rango que admite el "
                f"mercado [{self.tick_size}, {Decimal(1) - self.tick_size}]: la "
                f"orden se rechazaría."
            )
        if self.price % self.tick_size != 0:
            raise InvalidAmountError(
                f"el precio {self.price} no es múltiplo del salto {self.tick_size}: "
                f"el recinto rechaza la orden entera."
            )
        if self.size <= 0:
            raise InvalidAmountError(
                f"la orden necesita participaciones positivas, llegó {self.size}"
            )

    @property
    def cost(self) -> Decimal:
        """El lado del colateral: lo que se paga al comprar o se cobra al vender."""
        return EXACT.multiply(self.price, self.size)

    @property
    def shares(self) -> Decimal:
        """El lado de participaciones."""
        return self.size

    @property
    def is_buy(self) -> bool:
        return self.side is PredictionSide.BUY

    def describe(self) -> tuple[TransactionField, ...]:
        """Los campos que el usuario tiene que leer **antes** de firmar."""
        verbo = "Compras" if self.is_buy else "Vendes"
        dinero = "Pagas" if self.is_buy else "Cobras"
        return (
            ("Mercado", self.question),
            ("Resultado", f"{self.outcome_label} ({self.side.value})"),
            ("Red", self.chain),
            ("Recinto", self.venue_id),
            (verbo, f"{self.shares} participaciones"),
            ("Precio límite", f"{self.price} (salto {self.tick_size})"),
            (dinero, f"{self.cost} de colateral"),
            ("Tipo", self.order_type),
            (
                "Si acierta",
                f"cada participación paga 1: {self.shares} participaciones = "
                f"{self.shares} de colateral",
            ),
        )


@dataclass(frozen=True, slots=True)
class SignedPredictionOrder:
    """Una orden de predicción **firmada**, lista para publicarse en el recinto.

    Es el hermano de `SignedEvmTransaction` para un recinto que no liquida con
    una transacción propia sino con una firma que el operador ejecuta después.
    Comparte lo esencial: existe sólo durante la operación, no se guarda, y de la
    clave que la produjo no queda rastro salvo `signer`, que es la dirección
    pública y es justo lo que el usuario necesita ver.

    `payload` es el cuerpo exacto que se envía, ya serializado. Se guarda
    **serializado** y no como diccionario a propósito: la firma del recinto se
    calcula sobre esos bytes, así que si alguien reconstruyera el diccionario y
    volviera a serializarlo con otro orden de claves, la firma dejaría de
    corresponder a lo enviado. Con los bytes fijados aquí, lo que se firma y lo
    que se envía son lo mismo por construcción.
    """

    order: PredictionOrder
    signer: str
    signature: str
    #: El resumen EIP-712 que se firmó. **No es el identificador del recinto.**
    #:
    #: Se guarda porque es lo que permite comprobar la firma sin red —recuperar
    #: la dirección desde ella y ver que es la de la clave— y porque es el dato
    #: que hace reproducible lo firmado. El identificador con el que el recinto
    #: nombra la orden lo asigna **él** al aceptarla, y suponer que coincide con
    #: este resumen sería afirmar sin haberlo medido.
    digest: str
    #: El cuerpo exacto que se envía al recinto, ya serializado.
    #:
    #: Serializado y no como diccionario a propósito: la firma del recinto se
    #: calcula sobre **estos bytes**, así que reconstruir el diccionario y
    #: volver a serializarlo con otro orden de claves produciría unos bytes
    #: distintos con la misma firma. Fijándolos aquí, lo que se firma y lo que se
    #: envía son lo mismo por construcción.
    payload: str

    def __post_init__(self) -> None:
        if not self.signer:
            raise InvalidAmountError("una orden firmada necesita decir quién la firma")
        if not self.signature.startswith("0x"):
            raise InvalidAmountError("la firma de la orden no es hexadecimal")
        _require_tx_hash(self.digest, "el resumen firmado de la orden")

    def describe(self) -> tuple[TransactionField, ...]:
        return (*self.order.describe(), ("Firma", self.signer), ("Resumen", self.digest))


@dataclass(frozen=True, slots=True)
class SubmittedPredictionOrder:
    """Lo que el recinto contestó al aceptar una orden.

    Existe porque publicar **no** devuelve un recibo: no hay transacción, no hay
    bloque y no hay gas. Lo que devuelve es un identificador y un estado en el
    vocabulario del propio recinto —`live` está en el libro, `matched` ya se
    cruzó—, y las dos cosas son suyas: inventar aquí un estado propio o dar por
    hecho que el identificador es el resumen que se firmó sería escribir en el
    registro algo que nadie ha medido.

    `status` se guarda **tal cual lo dijo el recinto**, sin traducir. Un estado
    traducido a un vocabulario nuestro perdería justo la palabra que hay que
    buscar en su documentación el día que algo no cuadre.
    """

    order_id: str
    status: str

    def __post_init__(self) -> None:
        if not self.order_id:
            raise InvalidAmountError(
                "una orden aceptada necesita el identificador que le dio el recinto"
            )
        if not self.status:
            raise InvalidAmountError(
                "una orden aceptada necesita el estado en que la dejó el recinto"
            )


# --------------------------------------------------------------------------- #
# Acciones propuestas
# --------------------------------------------------------------------------- #
#: Par etiqueta/valor de un payload sin firmar. Es la forma en que la UI muestra
#: una transacción sin tener que saber de qué red es: cada tipo se describe a sí
#: mismo y la vista sólo pinta lo que recibe.
TransactionField = tuple[str, str]


def _require_confirmable(description: str) -> None:
    """Ninguna transacción se construye sin una descripción legible.

    El usuario tiene que poder leer qué está confirmando. Un payload sin
    descripción obliga a confiar en la máquina, que es lo contrario del
    principio rector del producto.
    """
    if not description:
        raise InvalidAmountError(
            "la transacción necesita una descripción legible: el usuario tiene "
            "que entender qué está confirmando"
        )


@dataclass(frozen=True, slots=True)
class TokenApproval:
    """A quién hay que autorizar para que un payload pueda mover un token.

    Existe porque «el router mueve el token» dejó de ser cierto. Se medió
    contra los contratos desplegados y hay dos caminos, no uno:

    - **Directo.** El router llama a `transferFrom` y el token se le autoriza a
      él. Es el caso de Uniswap V2 Router02 y del SwapRouter V1 de V3, que está
      en Ethereum, Optimism, Arbitrum y Polygon.
    - **Por Permit2.** El router **no** toca el token: se lo pide a Permit2, y
      Permit2 sólo lo entrega si el dueño se lo ha autorizado a él. Son dos
      permisos encadenados —el del ERC-20 contra Permit2 y el de Permit2 contra
      el router— y ninguno sustituye al otro. Es el caso del SwapRouter02, que
      es **el único router V3 que existe en Base**, y del Universal Router.

    Que esto viva en el payload y no en el ejecutor es lo que permite que la
    decisión la tome quien conoce el contrato —el motor, que acaba de leer su
    `factory()` y su `WETH9()`— y no una tabla de casos por red metida en el
    camino que firma.

    Un `approve` contra el contrato equivocado no falla al construirse: se
    firma, se emite, se paga el gas y el swap revierte. Por eso conviene que el
    dato viaje pegado al payload y no se deduzca en dos sitios distintos.
    """

    #: Contrato que ejecuta el `transferFrom` del token: el router del swap.
    spender: str
    #: Permit2, si el router cobra a través de él. `None` en el camino directo.
    via: str | None = None

    def __post_init__(self) -> None:
        if not self.spender:
            raise InvalidAmountError(
                "el permiso necesita el contrato que va a mover el token: sin él "
                "no se sabe a quién autorizar"
            )

    @property
    def target(self) -> str:
        """A quién se le da el `approve` del ERC-20. No siempre es el router.

        Es el único sitio donde esta decisión se toma: con Permit2 de por medio,
        el token se le autoriza a Permit2, y quien autoriza al router es Permit2.
        """
        return self.via or self.spender

    @property
    def is_chained(self) -> bool:
        """¿Hacen falta los dos permisos, o basta el del ERC-20?"""
        return self.via is not None

    def describe(self) -> TransactionField:
        if self.via is None:
            return ("Permiso del token", f"directo al router {self.spender}")
        return (
            "Permiso del token",
            f"encadenado por Permit2 ({self.via}) hacia el router {self.spender}: "
            f"son dos transacciones de permiso, no una",
        )


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    """Payload de transacción **sin firmar**, para revisión del usuario.

    No contiene ni referencia ninguna clave privada: la aplicación no firma ni
    emite transacciones. Este objeto existe para que el usuario pueda
    inspeccionar o exportar lo que se propone, nada más.

    Aquí `chain_id` **sí** es el entero EIP-155, y no la clave del registro como
    en el resto del dominio: forma parte del payload que se firmaría, y el
    formato de transacción lo exige numérico. Por eso este tipo sólo sirve para
    redes EVM; obtener el entero desde una clave de red es
    `ChainSpec.require_eip155_id()`, que falla explícitamente en las que no lo
    tienen. Para Solana, ver `UnsignedSolanaTransaction`.
    """

    chain_id: int
    to_address: str
    calldata: str
    value: TokenAmount
    description: str
    gas_limit: int | None = None
    #: Cómo hay que autorizar el token antes de que esto se pueda ejecutar.
    #:
    #: `None` —lo que hace todo motor que no diga otra cosa— significa el camino
    #: directo contra `to_address`, que es como funcionaba esto cuando el único
    #: router era uno que movía los tokens él mismo.
    approval: TokenApproval | None = None

    def __post_init__(self) -> None:
        if not self.to_address:
            raise InvalidAmountError("la transacción necesita una dirección destino")
        _require_confirmable(self.description)
        if self.value.raw < 0:
            raise InvalidAmountError("el valor de la transacción no puede ser negativo")

    @property
    def token_approval(self) -> TokenApproval:
        """El permiso a conceder, resolviendo el caso por omisión.

        Devuelve siempre un `TokenApproval` para que el ejecutor no tenga que
        ramificar: un payload que no declara nada se comporta como el camino
        directo contra su propio destino, que es lo que hacía antes de que este
        campo existiera.
        """
        return self.approval or TokenApproval(spender=self.to_address)

    def describe(self) -> tuple[TransactionField, ...]:
        """Campos para mostrar al usuario, en el orden en que se leen."""
        campos: tuple[TransactionField, ...] = (
            ("Red", f"EVM (chain id {self.chain_id})"),
            ("Destino", self.to_address),
            ("Valor", str(self.value)),
            ("Descripción", self.description),
        )
        # El permiso se enseña **sólo** cuando es encadenado. Enseñar el directo
        # sería ruido: es un `approve` de un token propio al contrato que va a
        # hacer el swap, y no hay nada que decidir sobre él. El encadenado sí,
        # porque son dos transacciones y una de ellas sobrevive a esta operación
        # hasta su caducidad — eso el usuario tiene que verlo antes de firmar.
        if self.approval is not None and self.approval.is_chained:
            campos += (self.approval.describe(),)
        return (*campos, ("Calldata", self.calldata))


@dataclass(frozen=True, slots=True)
class UnsignedSolanaTransaction:
    """Transacción de Solana sin firmar, tal como la entrega el agregado.

    **Hermano de `UnsignedTransaction`, no una generalización suya**, y la
    distinción es deliberada. En Solana no hay `chain_id` —la red va implícita y
    se ancla por el blockhash— ni `calldata`: la transacción es un mensaje
    versionado completo, serializado y en base64, que ya trae sus instrucciones
    y sus cuentas. Meterlo en `UnsignedTransaction` con los campos sobrantes en
    `None` convertiría dos invariantes hoy ciertas —«`chain_id` siempre es
    EIP-155», «`calldata` siempre es hexadecimal»— en dos campos que cada
    consumidor tendría que comprobar antes de usarlos. Cada tipo dice la verdad
    sobre su red y la unión `PlannedTransaction` es la que los unifica en la
    frontera.

    `last_valid_block_height` es la **caducidad**, y se toma de la fuente en vez
    de deducirse del payload. El blockhash está dentro del mensaje versionado y
    extraerlo obligaría a deserializarlo junto con sus *lookup tables*; la API ya
    publica la altura, que además es la cifra que el usuario necesita: «esto vale
    hasta la altura N». Una transacción de Solana que caduca es papel mojado, así
    que la caducidad no es un detalle de presentación, es parte del dato.
    """

    transaction_b64: str
    last_valid_block_height: int
    fee_payer: str
    description: str

    def __post_init__(self) -> None:
        if not self.transaction_b64:
            raise InvalidAmountError("la transacción de Solana llegó vacía")
        if self.last_valid_block_height <= 0:
            raise InvalidAmountError(
                "la transacción de Solana necesita la altura de bloque en la que "
                "caduca: sin ella no se puede saber si sigue siendo emitible"
            )
        if not self.fee_payer:
            raise InvalidAmountError("la transacción necesita el pagador de la comisión")
        _require_confirmable(self.description)

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Red", "Solana"),
            ("Pagador de comisión", self.fee_payer),
            ("Válida hasta la altura", str(self.last_valid_block_height)),
            ("Descripción", self.description),
            ("Transacción (base64)", self.transaction_b64),
        )


#: Lo que un motor puede devolver **construido y sin firmar**. La UI la consume
#: a través de `describe()`, así que añadir una red nueva no toca la vista.
#:
#: `PredictionOrder` está aquí y no en una unión aparte porque el diálogo de
#: confirmación tiene que poder enseñar exactamente lo mismo en los dos casos:
#: qué se va a firmar, campo por campo, antes de que exista una firma. Lo que
#: cambia es lo que pasa después —una transacción se emite, una orden se
#: publica—, y eso no lo decide esta unión.
type PlannedTransaction = (
    UnsignedTransaction | UnsignedSolanaTransaction | PredictionOrder
)


# --------------------------------------------------------------------------- #
# Ejecución — lo que ya no es un borrador
# --------------------------------------------------------------------------- #
#: Un hash de transacción: `0x` y 64 dígitos hexadecimales. Se valida porque un
#: hash inventado en un mensaje de error manda al usuario a un explorador donde
#: no hay nada, y eso es peor que no dar ninguno.
_TX_HASH_LENGTH: Final = 66

#: El prefijo de una transacción EIP-1559 firmada y serializada: tipo 2.
_EIP1559_PREFIX: Final = "0x02"


def _require_tx_hash(value: str, field_name: str) -> str:
    if len(value) != _TX_HASH_LENGTH or not value.startswith("0x"):
        raise InvalidAmountError(
            f"{field_name} no es un hash de transacción: «{value}». "
            f"Se espera 0x seguido de 64 dígitos hexadecimales."
        )
    try:
        int(value[2:], 16)
    except ValueError as error:
        raise InvalidAmountError(f"{field_name} no es hexadecimal: «{value}»") from error
    return value


class BroadcastStatus(StrEnum):
    """En qué estado quedó una transacción emitida."""

    #: Aceptada por el nodo y aún sin minar. Es el estado normal justo después
    #: de emitir: no se espera a que mine, porque eso puede tardar minutos y el
    #: usuario tiene que poder seguir usando la aplicación.
    PENDING = "pending"
    SUCCESS = "success"
    REVERTED = "reverted"
    #: No se llegó a saber: el nodo dejó de responder antes de dar un recibo.
    #: Es distinto de `PENDING` —aquí no se sabe si sigue viva— y decirlo así
    #: evita que el usuario dé por hecha una operación de la que no hay prueba.
    UNKNOWN = "unknown"

    @property
    def is_final(self) -> bool:
        return self in {BroadcastStatus.SUCCESS, BroadcastStatus.REVERTED}


class BridgeTrackState(StrEnum):
    """En qué va un puente después de emitir su origen."""

    PENDING = "pending"
    #: El proveedor devolvió los fondos a la dirección de origen.
    REFUNDING = "refunding"
    REFUNDED = "refunded"
    DONE = "done"
    FAILED = "failed"
    #: El proveedor aún no lo conoce —suele ser la primera ventana de indexación—.
    UNKNOWN = "unknown"

    @property
    def is_final(self) -> bool:
        return self in {
            BridgeTrackState.DONE,
            BridgeTrackState.REFUNDED,
            BridgeTrackState.FAILED,
        }


@dataclass(frozen=True, slots=True)
class BridgeProgress:
    """Lo que el proveedor dice de un puente en este momento, sin reinterpretarlo.

    `provider_status` y `substatus` son las cadenas tal cual llegan, para que la
    pantalla pueda enseñarlas junto a la traducción.
    """

    state: BridgeTrackState
    provider_status: str
    substatus: str
    message: str
    receiving_tx_hash: str | None = None
    received_raw: int | None = None
    received_chain: str | None = None


@dataclass(frozen=True, slots=True)
class SignedEvmTransaction:
    """Una transacción EVM **firmada**, lista para emitirse.

    Existe sólo aquí y no se guarda en ningún sitio: la firma convierte el
    payload en un instrumento al portador —quien la tenga puede emitirla— así que
    su vida es la de la operación y nada más.

    De la clave privada que la produjo **no hay rastro**: ni un campo, ni un
    hash, ni el `r`/`s`/`v` sueltos. El único vínculo es `from_address`, que es
    pública y es justo lo que el usuario necesita ver para saber de qué cartera
    sale el dinero.

    `nonce`, `gas_limit` y las comisiones viven aquí y no en
    `UnsignedTransaction` porque no son parte de la propuesta del motor: son
    estado de la red que se consultó en el último momento. Mezclarlos habría
    hecho que un payload de hace un minuto pareciera tener un nonce válido.
    """

    raw_hex: str
    tx_hash: str
    from_address: str
    chain_id: int
    nonce: int
    to_address: str
    value: TokenAmount
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    calldata: str
    description: str

    def __post_init__(self) -> None:
        if not self.raw_hex.startswith(_EIP1559_PREFIX):
            raise InvalidAmountError(
                f"la transacción firmada no empieza por {_EIP1559_PREFIX}: "
                f"no es una EIP-1559 serializada."
            )
        _require_tx_hash(self.tx_hash, "el hash de la transacción")
        if self.nonce < 0:
            raise InvalidAmountError(f"el nonce no puede ser negativo, llegó {self.nonce}")
        if self.gas_limit <= 0:
            raise InvalidAmountError(
                f"el límite de gas debe ser positivo, llegó {self.gas_limit}. "
                f"Sin él la transacción no se puede firmar."
            )
        if self.max_fee_per_gas <= 0:
            raise InvalidAmountError(
                f"el precio máximo por gas debe ser positivo, llegó {self.max_fee_per_gas}"
            )
        if self.max_fee_per_gas < self.max_priority_fee_per_gas:
            raise InvalidAmountError(
                f"el precio máximo por gas ({self.max_fee_per_gas}) es menor que la "
                f"propina ({self.max_priority_fee_per_gas}): la transacción nunca "
                f"sería aceptable y el nodo la rechazaría."
            )
        _require_confirmable(self.description)

    @property
    def max_cost_wei(self) -> int:
        """Lo máximo que puede costar en gas, en wei. Es el techo, no la cuenta."""
        return self.gas_limit * self.max_fee_per_gas

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Red", f"EVM (chain id {self.chain_id})"),
            ("Firma", self.from_address),
            ("Destino", self.to_address),
            ("Valor", str(self.value)),
            ("Nonce", str(self.nonce)),
            ("Límite de gas", str(self.gas_limit)),
            ("Coste máximo en gas", f"{self.max_cost_wei} wei"),
            ("Hash", self.tx_hash),
            ("Descripción", self.description),
        )


@dataclass(frozen=True, slots=True)
class BroadcastReceipt:
    """Qué pasó al emitir. Es la prueba de que la operación ocurrió."""

    tx_hash: str
    chain: str
    status: BroadcastStatus
    observed_at: datetime
    block_number: int | None = None
    #: Motivo del fallo si la red la revirtió, tal cual lo dio el nodo.
    reason: str = ""

    def __post_init__(self) -> None:
        _require_tx_hash(self.tx_hash, "el hash del recibo")
        _require_aware(self.observed_at, "el momento del recibo")
        if self.status is BroadcastStatus.REVERTED and not self.reason:
            raise InvalidAmountError(
                "un recibo revertido tiene que traer el motivo: sin él no se puede "
                "saber si fue el mercado o un parámetro mal puesto."
            )

    @property
    def is_proof(self) -> bool:
        """Si esto ya es prueba suficiente para anotarlo en el registro.

        `PENDING` cuenta: la transacción existe y es pública, aunque aún no haya
        minado. `UNKNOWN` no, porque anotar como hecha una operación de la que no
        se sabe nada es exactamente lo que un registro no debe hacer.
        """
        return self.status is not BroadcastStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Lo que se propone ejecutar, en los términos que la política evalúa.

    `notional` es lo que **se entrega**, y por eso tiene que ser una de las dos
    patas del par: es un hecho, no una estimación.

    `reference_value` es cuánto vale eso en la moneda en la que están escritos
    los topes —la stablecoin de referencia de la red—, y sólo aparece cuando el
    par no toca esa moneda. Ahí es donde está el trabajo: «5 000 por operación»
    tiene que querer decir lo mismo para un par de WETH que para uno de un token
    de 18 decimales que nadie conoce, y comparar el importe entregado contra un
    tope expresado en otra unidad sería comparar peras con manzanas —o peor,
    dejar que un token carísimo se cuele por debajo de un tope pensado para
    dólares—. Cuando el par **sí** toca la moneda de referencia no hace falta
    ninguna valoración: la pata que coincide es el valor exacto, y `None` aquí
    significa exactamente eso.
    """

    quote: Quote
    recipient: str
    notional: TokenAmount
    engine_id: str
    #: El mismo importe, valorado en la moneda de referencia de la red. `None`
    #: cuando `notional` **ya** está en esa moneda, que es el caso de siempre.
    reference_value: TokenAmount | None = None

    def __post_init__(self) -> None:
        if not self.recipient:
            raise InvalidAmountError("una ejecución necesita destinatario")
        if self.notional.raw <= 0:
            raise InvalidAmountError(
                f"el importe de referencia debe ser positivo, llegó {self.notional}"
            )
        if self.reference_value is not None and self.reference_value.raw <= 0:
            raise InvalidAmountError(
                f"la valoración en la moneda de referencia debe ser positiva, llegó "
                f"{self.reference_value}"
            )
        # `TokenAmount` no lleva la red —sólo símbolo y escala—, así que la
        # coherencia se comprueba contra las dos patas del par, que sí la llevan.
        # El importe entregado es siempre una de ellas. Comparar por `symbol` y
        # `decimals` es lo que el resto del dominio usa para decidir si dos
        # cantidades son sumables.
        legs = (self.quote.pair.base, self.quote.pair.quote)
        if not any(
            leg.symbol == self.notional.symbol and leg.decimals == self.notional.decimals
            for leg in legs
        ):
            raise CurrencyMismatchError(
                f"el importe de referencia ({self.notional}) no es ninguna de las dos "
                f"patas de {self.quote.pair.symbol}: no se puede comparar contra un "
                f"tope expresado en otra unidad."
            )

    @property
    def measured(self) -> TokenAmount:
        """El importe que se mide contra los topes, **en la unidad del tope**.

        Es la única cifra que ve la política. Se prefiere la valoración cuando
        existe y se cae al importe entregado cuando no, que es el caso en el que
        los dos son el mismo número y no hay nada que valorar.
        """
        return self.reference_value or self.notional

    @property
    def tokens(self) -> tuple[str, str]:
        """Los dos símbolos que toca la operación, para la lista blanca."""
        return (self.quote.pair.base.symbol, self.quote.pair.quote.symbol)

    @property
    def chain(self) -> str:
        """La red, sin obligar a quien la pregunta a conocer la forma de un `Quote`.

        La política comprueba la red contra su lista blanca y no necesita nada
        más del par; exponerlo así es lo que permite que la misma comprobación
        sirva para un swap y para una orden de predicción sin duplicarla.
        """
        return self.quote.pair.chain

    @property
    def label(self) -> str:
        """Cómo se nombra esta operación en el registro y en la traza."""
        return self.quote.pair.symbol

    @property
    def counts_towards_limits(self) -> bool:
        """Un swap **entrega** `notional`, así que el tope lo acota. Siempre sí."""
        return True

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Par", self.quote.pair.symbol),
            ("Entregas", str(self.quote.amount_in)),
            ("Recibes", str(self.quote.amount_out)),
            ("Importe de referencia", str(self.notional)),
            ("Motor", self.engine_id),
            ("Destino", self.recipient),
        )


@dataclass(frozen=True, slots=True)
class PredictionOrderIntent:
    """Lo que se propone publicar en un recinto de predicción, para la política.

    **Hermano de `ExecutionIntent`, no una variante suya.** Un swap tiene dos
    patas de tokens y un par; una orden tiene un resultado, un precio y unas
    participaciones, y meterla en `ExecutionIntent` con los campos sobrantes en
    `None` convertiría invariantes hoy ciertas en campos que cada consumidor
    tendría que comprobar. Lo que **sí** comparten es lo que la política mira, y
    eso se dice con un `Protocol` en `app/execution_policy.py`, no copiando el
    método de comprobación.

    El importe que se mide contra los topes es el lado **en colateral**, tanto al
    comprar como al vender. Es la unidad en la que están escritos los topes y es
    un dato exacto —precio por participaciones—, así que no hace falta valorar
    nada: lo que se paga al comprar y lo que se cobra al vender ya están en
    dólares, y el tope diario suma así lo de predicción junto con lo de los
    swaps, que es lo que se quiere.
    """

    order: PredictionOrder
    recipient: str
    #: El importe del lado del colateral, en la stablecoin de la red. `None` en
    #: `reference_value` significa que **no hace falta** ninguna valoración: ya
    #: está en la unidad del tope.
    notional: TokenAmount
    engine_id: str
    reference_value: TokenAmount | None = None

    def __post_init__(self) -> None:
        if not self.recipient:
            raise InvalidAmountError("una orden necesita destinatario")
        if self.notional.raw <= 0:
            raise InvalidAmountError(
                f"el importe de referencia debe ser positivo, llegó {self.notional}"
            )
        if self.notional.as_decimal() < self.order.cost:
            # El importe que se mide contra los topes **tiene que cubrir** el
            # coste de la orden. Si no lo cubriera, el tope estaría midiendo un
            # número más pequeño que el dinero que de verdad se mueve, que es la
            # forma de que un tope deje de ser un tope.
            raise CurrencyMismatchError(
                f"el importe de referencia ({self.notional}) es menor que el coste "
                f"de la orden ({self.order.cost}): el tope estaría midiendo de menos."
            )

    @property
    def chain(self) -> str:
        return self.order.chain

    @property
    def label(self) -> str:
        """Lo que se escribe en el registro: el mercado y el resultado elegido.

        La pregunta sola no basta: un mercado con dos resultados produce dos
        operaciones opuestas, y un asiento que dijera sólo el mercado no
        permitiría saber cuál de las dos se hizo.
        """
        return f"{self.order.question} — {self.order.outcome_label}"

    @property
    def tokens(self) -> tuple[str, ...]:
        """Los símbolos que toca, para la lista blanca.

        Sólo el colateral. Las participaciones son un ERC-1155 que no está en el
        catálogo de tokens y que cambia con cada mercado: meterlas en la lista
        blanca obligaría a mantener a mano una lista de resultados que se renueva
        cada pocos minutos, y una lista blanca que se mantiene a mano deja de
        estar actualizada el día que más importa.
        """
        return (self.notional.symbol,)

    @property
    def measured(self) -> TokenAmount:
        return self.reference_value or self.notional

    @property
    def counts_towards_limits(self) -> bool:
        """Una orden **compromete** el colateral, tanto al comprar como al vender.

        Al vender el colateral entra y las participaciones salen, así que el
        signo del dinero depende del lado — pero el tope no acota el saldo, acota
        lo que se **compromete**: ambos lados de una orden son importes que se
        ponen en juego y pueden perderse enteros si el mercado se mueve. Por eso
        los dos cuentan, y por eso esta respuesta no mira `self.order.side`.
        """
        return True

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            *self.order.describe(),
            ("Importe de referencia", str(self.notional)),
            ("Motor", self.engine_id),
            ("Destino", self.recipient),
        )


@dataclass(frozen=True, slots=True)
class WithdrawIntent:
    """Sacar fondos de la cartera: la operación más simple y la más irreversible.

    Cuarto hermano de `ExecutionIntent`, `PredictionOrderIntent` y
    `PredictionRedeemIntent`, y el único que **no pasa por ningún motor**. Un
    swap lo construye un motor que cotiza; esto es una transferencia y la
    construye la propia aplicación, porque no hay nada que cotizar: se manda lo
    que se manda.

    **Cuenta para los topes, y es el caso en el que más claro está.** Los topes
    existen para acotar lo que **sale** de la cartera, y esto es exactamente eso,
    sin nada alrededor: no hay un par, ni un contrato de por medio, ni una
    posición que cambie de forma. Un tope que no acotara una retirada sería un
    tope que se puede vaciar la cartera sin tocarlo.

    ### Por qué el destinatario no puede ser el que firma

    Mandarse fondos a uno mismo es una transferencia que gasta gas y no mueve
    nada. No es peligroso, pero sí es siempre un error —una dirección pegada en
    la casilla equivocada— y el momento de decirlo es antes de firmar, no
    después. Se rechaza aquí, en el dominio, para que no dependa de que cada
    caso de uso se acuerde de comprobarlo.
    """

    chain: str
    token: Token
    amount: TokenAmount
    #: A dónde va el dinero. Es la dirección de destino, no la de la cartera.
    recipient: str
    #: Quién firma. Se guarda aparte de `recipient` justamente para poder
    #: comprobar que no coinciden, y para que el asiento del registro diga de
    #: dónde salió además de a dónde fue.
    source: str
    engine_id: str
    reference_value: TokenAmount | None = None

    def __post_init__(self) -> None:
        if not self.recipient:
            raise InvalidAmountError("una retirada necesita destinatario")
        if not self.source:
            raise InvalidAmountError("una retirada necesita saber de qué cartera sale")
        if self.amount.raw <= 0:
            raise InvalidAmountError(
                f"el importe de una retirada debe ser positivo, llegó {self.amount}"
            )
        if self.amount.symbol != self.token.symbol or self.amount.decimals != self.token.decimals:
            # Sin esto, la cantidad y el token del que dice ser podrían divergir:
            # `TokenAmount` no lleva la red ni la dirección, sólo símbolo y
            # escala, así que es aquí donde se comprueba que hablan del mismo.
            raise CurrencyMismatchError(
                f"el importe ({self.amount}) no corresponde al token {self.token.symbol} "
                f"de {self.chain} ({self.token.decimals} decimales)."
            )
        if self.token.chain != self.chain:
            raise CurrencyMismatchError(
                f"el token {self.token.symbol} es de «{self.token.chain}» y la retirada "
                f"dice «{self.chain}»."
            )
        if self.recipient.strip().lower() == self.source.strip().lower():
            raise InvalidAmountError(
                "el destinatario de la retirada es la misma cartera que firma: eso "
                "sólo gasta gas y no mueve nada, así que casi siempre es una "
                "dirección pegada en la casilla equivocada."
            )
        if self.reference_value is not None and self.reference_value.raw <= 0:
            raise InvalidAmountError(
                f"la valoración en la moneda de referencia debe ser positiva, llegó "
                f"{self.reference_value}"
            )

    @property
    def label(self) -> str:
        """Lo que se escribe en el registro: el token y a dónde fue."""
        return f"Retirada {self.amount.symbol} → {self.recipient}"

    @property
    def tokens(self) -> tuple[str, ...]:
        """El símbolo que sale, para la lista blanca.

        Sólo uno, y es el que de verdad se mueve: una retirada no toca ningún
        otro token, así que pedir permiso por otros sería ampliar la lista blanca
        sin motivo.
        """
        return (self.token.symbol,)

    @property
    def notional(self) -> TokenAmount:
        """Lo que sale, en la unidad del propio token.

        Es `amount` sin más, y se declara aparte de `measured` por la razón de
        siempre: `measured` es esa misma cifra traducida a la moneda del tope, y
        confundirlas es lo que haría que el registro anotara un importe que no
        corresponde al token que se movió.
        """
        return self.amount

    @property
    def measured(self) -> TokenAmount:
        """El importe que se compara contra los topes, en la unidad del tope."""
        return self.reference_value or self.amount

    @property
    def counts_towards_limits(self) -> bool:
        """Siempre. Es, literalmente, dinero saliendo de la cartera."""
        return True

    def describe(self) -> tuple[TransactionField, ...]:
        origen = (
            "la moneda nativa de la red"
            if self.token.address is None
            else f"el contrato {self.token.address}"
        )
        return (
            ("Retirada de", str(self.amount)),
            ("Token", f"{self.token.symbol} ({origen})"),
            ("Red", self.chain),
            ("Sale de", self.source),
            ("Va a", self.recipient),
            ("Importe de referencia", str(self.measured)),
        )


@dataclass(frozen=True, slots=True)
class PredictionRedeemIntent:
    """Lo que se propone **cobrar**, en los términos que la política evalúa.

    Tercer hermano de `ExecutionIntent` y `PredictionOrderIntent`, y el único de
    los tres que va en la dirección contraria: los otros dos **entregan** dinero,
    y éste lo **recibe**. Eso no es una diferencia de matiz, porque los topes
    están escritos para acotar lo que sale:

    - `max_quote_per_trade` y `max_quote_per_day` dicen «no comprometas más de
      esto». Una redención no compromete nada: cambia participaciones que ya se
      pagaron por el colateral que valen. Aplicarle el tope tendría la
      consecuencia absurda de **impedir cobrar** una posición grande —cuanto más
      ganada, más bloqueada—, y dejaría el dinero del usuario encerrado en el
      contrato justo cuando por fin se puede sacar.
    - Por eso `notional` aquí es el **cobro**, y la política lo mira para
      anotarlo pero **no** lo compara contra los topes. Lo que sí se comprueba
      entero es todo lo demás —modo, red, colateral y motor—, que es lo que
      decide si esta operación puede existir.

    El nombre del campo no cambia para que este objeto siga satisfaciendo
    `ExecutableIntent` —la política, el registro y el diálogo lo leen igual que a
    los otros dos—, y lo que lo distingue es `counts_towards_limits`, que aquí es
    falso. Ése es el dato que `AutonomyPolicy.check_intent` consulta para saltarse
    el importe, y vive en el intento y no en una rama por tipo dentro de la
    política: así el día que aparezca una cuarta forma de operar, la decisión se
    toma donde se sabe la respuesta en vez de en una lista que hay que recordar
    ampliar.
    """

    position: PredictionPosition
    recipient: str
    #: Lo que se cobra, en la stablecoin del recinto. Es `shares` —cada
    #: participación ganadora paga 1— y ya está en la unidad del tope, así que no
    #: hay nada que valorar.
    notional: TokenAmount
    engine_id: str
    reference_value: TokenAmount | None = None

    def __post_init__(self) -> None:
        if not self.recipient:
            raise InvalidAmountError("una redención necesita destinatario")
        if self.notional.raw <= 0:
            raise InvalidAmountError(
                f"el importe a cobrar debe ser positivo, llegó {self.notional}"
            )
        if self.notional.as_decimal() < self.position.payout:
            # Mismo sentido que en la orden, y por la misma razón: si el importe
            # anotado no cubriera lo que de verdad se cobra, el asiento del
            # registro describiría una operación más pequeña que la que pasó.
            raise CurrencyMismatchError(
                f"el importe a cobrar ({self.notional}) es menor que lo que paga "
                f"la posición ({self.position.payout}): el asiento describiría de menos."
            )

    @property
    def chain(self) -> str:
        return self.position.venue.chain

    @property
    def label(self) -> str:
        """Lo que se escribe en el registro: el mercado y el resultado cobrado.

        Lleva delante la palabra «cobro» porque este asiento es el único que
        **entra** dinero, y un renglón con la misma forma que los de gasto se
        sumaría con ellos al leer el fichero. Se distingue por el texto, ya que
        el registro es lo que un humano lee para reconstruir qué pasó.
        """
        return f"cobro {self.position.question} — {self.position.outcome_label}"

    @property
    def tokens(self) -> tuple[str, ...]:
        """Los símbolos que toca, para la lista blanca.

        Sólo el colateral, por la misma razón que en la orden: las
        participaciones son un ERC-1155 que no está en el catálogo y que cambia
        con cada mercado.
        """
        return (self.notional.symbol,)

    @property
    def measured(self) -> TokenAmount:
        return self.reference_value or self.notional

    @property
    def counts_towards_limits(self) -> bool:
        """**No.** Es la única operación de las tres que no compromete nada.

        El dinero va en la dirección contraria a la que el tope acota. Ver el
        porqué entero en el docstring de la clase.
        """
        return False

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Mercado", self.position.question),
            ("Resultado", self.position.outcome_label),
            ("Red", self.chain),
            ("Recinto", self.position.venue.venue_id),
            ("Participaciones", f"{self.position.shares:f}"),
            ("Cobras", str(self.notional)),
            ("Motor", self.engine_id),
            ("Destino", self.recipient),
        )
