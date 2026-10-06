"""Aritmética monetaria exacta.

Regla no negociable del proyecto: **ningún valor económico pasa por `float`**.

- Las cantidades de token se representan como entero en la unidad mínima
  (`raw`, p. ej. wei) más el número de `decimals` del token.
- Los ratios, precios y porcentajes se representan como `Decimal`.
- Las comisiones y desviaciones se representan como `BasisPoints` (entero).

`TokenAmount` no define `__float__` de forma deliberada: convertir a binario de
coma flotante debe requerir una decisión consciente del autor del código.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Final, Self

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError

#: Precisión interna de las divisiones. Holgada frente a los 18 decimales
#: habituales en EVM para que los redondeos intermedios no se acumulen.
ARITHMETIC_PRECISION: Final = 60

#: Contexto decimal único del dominio. Bancario (half-even) para no sesgar
#: sistemáticamente al alza una serie larga de redondeos.
EXACT: Final = Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_EVEN)

#: 1 punto básico = 0,01 %.
BPS_DENOMINATOR: Final = 10_000


def _scale(decimals: int) -> Decimal:
    return Decimal(10) ** decimals


@dataclass(frozen=True, slots=True)
class TokenAmount:
    """Cantidad exacta de un token.

    `raw` es el entero en la unidad mínima; `decimals` su escala. Dos cantidades
    sólo se combinan si coinciden `symbol` y `decimals`.
    """

    raw: int
    decimals: int
    symbol: str

    def __post_init__(self) -> None:
        # `type(...) is int` y no `isinstance`: rechaza `bool` (subclase de int)
        # y cualquier `float` que se cuele desde JSON o desde la UI.
        if type(self.raw) is not int:
            raise InvalidAmountError(
                f"raw debe ser int exacto, llegó {type(self.raw).__name__}={self.raw!r}"
            )
        if type(self.decimals) is not int or self.decimals < 0:
            raise InvalidAmountError(f"decimals debe ser un int >= 0, llegó {self.decimals!r}")
        if not self.symbol:
            raise InvalidAmountError("symbol no puede estar vacío")

    # ----------------------------------------------------------------- build #
    @classmethod
    def zero(cls, decimals: int, symbol: str) -> Self:
        return cls(raw=0, decimals=decimals, symbol=symbol)

    @classmethod
    def from_decimal(
        cls,
        value: Decimal | int | str,
        decimals: int,
        symbol: str,
        *,
        rounding: str | None = None,
    ) -> Self:
        """Construye desde una cantidad en unidades humanas.

        Si el valor tiene más precisión de la que el token admite, falla en vez
        de redondear en silencio. Pasa `rounding` explícito para autorizarlo.
        """
        scaled = EXACT.multiply(Decimal(value), _scale(decimals))
        integral = scaled.to_integral_value(rounding=rounding or ROUND_HALF_EVEN)
        if rounding is None and integral != scaled:
            raise InvalidAmountError(
                f"{value} no es representable con {decimals} decimales sin pérdida; "
                f"pasa rounding= explícito si el redondeo es aceptable"
            )
        return cls(raw=int(integral), decimals=decimals, symbol=symbol)

    # ------------------------------------------------------------------ read #
    def as_decimal(self) -> Decimal:
        """Cantidad en unidades humanas. Exacta: `raw` y la escala son enteros."""
        return EXACT.divide(Decimal(self.raw), _scale(self.decimals))

    @property
    def is_zero(self) -> bool:
        return self.raw == 0

    @property
    def is_positive(self) -> bool:
        return self.raw > 0

    # --------------------------------------------------------------- compose #
    def _require_compatible(self, other: TokenAmount) -> None:
        if self.symbol != other.symbol or self.decimals != other.decimals:
            raise CurrencyMismatchError(
                f"no se pueden combinar {self.symbol}({self.decimals}) "
                f"y {other.symbol}({other.decimals})"
            )

    def __add__(self, other: TokenAmount) -> Self:
        self._require_compatible(other)
        return type(self)(raw=self.raw + other.raw, decimals=self.decimals, symbol=self.symbol)

    def __sub__(self, other: TokenAmount) -> Self:
        self._require_compatible(other)
        return type(self)(raw=self.raw - other.raw, decimals=self.decimals, symbol=self.symbol)

    def __neg__(self) -> Self:
        return type(self)(raw=-self.raw, decimals=self.decimals, symbol=self.symbol)

    def scaled_by(self, factor: Decimal | int | str, *, rounding: str = ROUND_HALF_EVEN) -> Self:
        """Multiplica por un factor adimensional (p. ej. 1 - fee)."""
        product = EXACT.multiply(Decimal(self.raw), Decimal(factor))
        return type(self)(
            raw=int(product.to_integral_value(rounding=rounding)),
            decimals=self.decimals,
            symbol=self.symbol,
        )

    def price_against(self, quote: TokenAmount) -> Price:
        """Precio implícito de este token expresado en `quote`."""
        if self.is_zero:
            raise InvalidAmountError("no se puede derivar un precio con cantidad base cero")
        return Price(
            value=EXACT.divide(quote.as_decimal(), self.as_decimal()),
            base=self.symbol,
            quote=quote.symbol,
        )

    # ------------------------------------------------------------- ordenar  #
    def __lt__(self, other: TokenAmount) -> bool:
        self._require_compatible(other)
        return self.raw < other.raw

    def __le__(self, other: TokenAmount) -> bool:
        self._require_compatible(other)
        return self.raw <= other.raw

    def __gt__(self, other: TokenAmount) -> bool:
        self._require_compatible(other)
        return self.raw > other.raw

    def __ge__(self, other: TokenAmount) -> bool:
        self._require_compatible(other)
        return self.raw >= other.raw

    def __str__(self) -> str:
        return f"{self.as_decimal():f} {self.symbol}"


@dataclass(frozen=True, slots=True)
class Price:
    """Unidades de `quote` por cada unidad de `base`."""

    value: Decimal
    base: str
    quote: str

    def __post_init__(self) -> None:
        if not self.value.is_finite():
            raise InvalidAmountError(f"precio no finito: {self.value}")
        if self.value <= 0:
            raise InvalidAmountError(f"el precio debe ser positivo, llegó {self.value}")

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"

    def inverted(self) -> Price:
        return Price(
            value=EXACT.divide(Decimal(1), self.value),
            base=self.quote,
            quote=self.base,
        )

    def difference_from(self, reference: Price) -> BasisPoints:
        """Desviación de este precio respecto a `reference`, en puntos básicos.

        Positivo = este precio es más alto que la referencia.
        """
        if (self.base, self.quote) != (reference.base, reference.quote):
            raise CurrencyMismatchError(
                f"no se pueden comparar precios de {self.pair} y {reference.pair}"
            )
        ratio = EXACT.divide(EXACT.subtract(self.value, reference.value), reference.value)
        return BasisPoints.from_ratio(ratio)

    def __str__(self) -> str:
        return f"{self.value:f} {self.quote}/{self.base}"


@dataclass(frozen=True, slots=True, order=True)
class BasisPoints:
    """Puntos básicos. 1 bp = 0,01 % = 1/10 000."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise InvalidAmountError(f"bps debe ser int, llegó {type(self.value).__name__}")

    @classmethod
    def from_ratio(cls, ratio: Decimal | int | str, *, rounding: str = ROUND_HALF_EVEN) -> Self:
        scaled = EXACT.multiply(Decimal(ratio), Decimal(BPS_DENOMINATOR))
        return cls(value=int(scaled.to_integral_value(rounding=rounding)))

    @classmethod
    def from_percent(cls, percent: Decimal | int | str) -> Self:
        return cls.from_ratio(EXACT.divide(Decimal(percent), Decimal(100)))

    def as_ratio(self) -> Decimal:
        return EXACT.divide(Decimal(self.value), Decimal(BPS_DENOMINATOR))

    def as_percent(self) -> Decimal:
        return EXACT.divide(Decimal(self.value), Decimal(100))

    def portion_of(self, amount: TokenAmount) -> TokenAmount:
        """La parte de `amount` que representan estos bps."""
        return amount.scaled_by(self.as_ratio())

    def __abs__(self) -> Self:
        return type(self)(value=abs(self.value))

    def __neg__(self) -> Self:
        return type(self)(value=-self.value)

    def __str__(self) -> str:
        return f"{self.value} bps ({self.as_percent():f} %)"
