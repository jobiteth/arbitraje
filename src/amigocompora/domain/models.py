"""Entidades y value objects del dominio.

Todo es inmutable (`frozen=True`) y se valida en construcción: un objeto que
existe es un objeto coherente, así que las capas superiores no vuelven a
comprobar invariantes. Las cantidades usan siempre `TokenAmount`/`Decimal`,
nunca `float` (ver `money`).
"""

from __future__ import annotations

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
        return f"{self.symbol} {self.address[:10]}…"

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
    """Un resultado posible y su precio en [0, 1], que es su probabilidad."""

    label: str
    price: Decimal

    def __post_init__(self) -> None:
        if not self.label:
            raise InvalidAmountError("el resultado necesita una etiqueta")
        if not self.price.is_finite() or not (Decimal(0) <= self.price <= Decimal(1)):
            raise InvalidAmountError(
                f"el precio de «{self.label}» debe estar en [0, 1], llegó {self.price}"
            )

    @property
    def implied_probability(self) -> Decimal:
        return self.price

    @property
    def implied_percent(self) -> Decimal:
        return EXACT.multiply(self.price, Decimal(100))


@dataclass(frozen=True, slots=True)
class PredictionMarket:
    """Un mercado de predicción con sus resultados mutuamente excluyentes."""

    market_id: str
    venue: Venue
    question: str
    outcomes: tuple[MarketOutcome, ...]
    observed_at: datetime
    closes_at: datetime | None = None

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


#: Lo que un motor de swap puede devolver. La UI la consume a través de
#: `describe()`, así que añadir una red nueva no toca la vista.
type PlannedTransaction = UnsignedTransaction | UnsignedSolanaTransaction


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

    def describe(self) -> tuple[TransactionField, ...]:
        return (
            ("Par", self.quote.pair.symbol),
            ("Entregas", str(self.quote.amount_in)),
            ("Recibes", str(self.quote.amount_out)),
            ("Importe de referencia", str(self.notional)),
            ("Motor", self.engine_id),
            ("Destino", self.recipient),
        )
