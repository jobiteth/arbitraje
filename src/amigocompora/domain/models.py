"""Entidades y value objects del dominio.

Todo es inmutable (`frozen=True`) y se valida en construcción: un objeto que
existe es un objeto coherente, así que las capas superiores no vuelven a
comprobar invariantes. Las cantidades usan siempre `TokenAmount`/`Decimal`,
nunca `float` (ver `money`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        if self.base.symbol == self.quote.symbol:
            raise CurrencyMismatchError(f"un par no puede ser {self.base.symbol} contra sí mismo")
        if self.base.chain != self.quote.chain:
            raise CurrencyMismatchError(
                f"{self.base.symbol} está en la red {self.base.chain} "
                f"y {self.quote.symbol} en {self.quote.chain}: no hay pool que los una"
            )

    @property
    def symbol(self) -> str:
        return f"{self.base.symbol}/{self.quote.symbol}"

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
    price_impact_bps: BasisPoints
    observed_at: datetime
    liquidity: TokenAmount | None = None
    #: Procedencia de `fee_bps`, y `None` cuando `fee_bps` lo es. Por defecto lo
    #: más honesto que puede afirmar un motor genérico; cada motor real lo ajusta
    #: a lo que de verdad publica su fuente.
    fee_basis: Measurement | None = Measurement.REPORTED
    #: Procedencia de `price_impact_bps`, que siempre existe.
    impact_basis: Measurement = Measurement.DERIVED
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
        """
        return (self.fee_basis is None or self.fee_basis.is_exact) and self.impact_basis.is_exact

    @property
    def price(self) -> Price:
        """Precio efectivo de ejecución, neto de comisiones e impacto."""
        return self.amount_in.price_against(self.amount_out)

    def __str__(self) -> str:
        return f"{self.venue.name}: {self.amount_in} → {self.amount_out} ({self.price})"


@dataclass(frozen=True, slots=True)
class PriceComparison:
    """Las cotizaciones de un mismo `amount_in` en varios venues."""

    pair: TradingPair
    amount_in: TokenAmount
    quotes: tuple[Quote, ...]

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

    def __post_init__(self) -> None:
        if not self.to_address:
            raise InvalidAmountError("la transacción necesita una dirección destino")
        _require_confirmable(self.description)
        if self.value.raw < 0:
            raise InvalidAmountError("el valor de la transacción no puede ser negativo")

    def describe(self) -> tuple[TransactionField, ...]:
        """Campos para mostrar al usuario, en el orden en que se leen."""
        return (
            ("Red", f"EVM (chain id {self.chain_id})"),
            ("Destino", self.to_address),
            ("Valor", str(self.value)),
            ("Descripción", self.description),
            ("Calldata", self.calldata),
        )


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
