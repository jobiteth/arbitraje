"""Aritmética de pool de producto constante (`x·y = k`).

Es la fórmula de Uniswap V2 y de sus clones, así que no es código de juguete:
los motores de M2 que lean reservas reales de un pool V2 usarán esta misma
función. Vive en `engines/` porque es conocimiento de un tipo concreto de DEX,
no una regla del dominio.

Dos decisiones que importan:

- **Comisión e impacto de precio se calculan por separado.** El impacto se mide
  sobre la salida *sin* comisión, de modo que la UI pueda mostrar «te cobran 30
  bps» y «tu orden mueve el precio 45 bps» como dos cifras distintas. Sumarlas
  en una sola oculta cuál de las dos domina, que es justo lo que el usuario
  necesita saber para decidir si parte la orden.
- **La salida se redondea hacia abajo.** Una cotización nunca debe prometer más
  de lo que se recibiría.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from amigocompora.domain.errors import CurrencyMismatchError, InvalidAmountError
from amigocompora.domain.money import EXACT, BasisPoints, Price, TokenAmount


@dataclass(frozen=True, slots=True)
class ConstantProductPool:
    """Un pool con dos reservas y una comisión."""

    reserve_base: TokenAmount
    reserve_quote: TokenAmount
    fee_bps: BasisPoints

    def __post_init__(self) -> None:
        if not self.reserve_base.is_positive or not self.reserve_quote.is_positive:
            raise InvalidAmountError("las reservas del pool deben ser positivas")
        if self.fee_bps.value < 0:
            raise InvalidAmountError(f"la comisión no puede ser negativa: {self.fee_bps}")

    @property
    def mid_price(self) -> Price:
        """Precio sin operar: el ratio de reservas. No alcanzable para orden > 0."""
        return self.reserve_base.price_against(self.reserve_quote)

    def _require_base(self, amount_in: TokenAmount) -> None:
        if amount_in.symbol != self.reserve_base.symbol:
            raise CurrencyMismatchError(
                f"este pool recibe {self.reserve_base.symbol}, no {amount_in.symbol}"
            )
        if not amount_in.is_positive:
            raise InvalidAmountError("la cantidad de entrada debe ser positiva")

    def _output_raw(self, amount_in_raw: int) -> int:
        """`R_q · Δb / (R_b + Δb)`, en unidades mínimas.

        Correcto aunque base y quote tengan distinto número de decimales: las
        reservas entran en sus propias unidades mínimas y el resultado sale en
        las del quote.
        """
        numerator = EXACT.multiply(Decimal(self.reserve_quote.raw), Decimal(amount_in_raw))
        denominator = Decimal(self.reserve_base.raw + amount_in_raw)
        quotient = EXACT.divide(numerator, denominator)
        return int(quotient.to_integral_value(rounding=ROUND_DOWN))

    def output_for(self, amount_in: TokenAmount) -> TokenAmount:
        """Lo que se recibe de verdad: con comisión y con impacto de precio."""
        self._require_base(amount_in)
        after_fee = amount_in.scaled_by(
            EXACT.subtract(Decimal(1), self.fee_bps.as_ratio()),
            rounding=ROUND_DOWN,
        )
        return TokenAmount(
            raw=self._output_raw(after_fee.raw),
            decimals=self.reserve_quote.decimals,
            symbol=self.reserve_quote.symbol,
        )

    def output_without_fee(self, amount_in: TokenAmount) -> TokenAmount:
        """Salida con impacto de precio pero sin comisión. Base del cálculo de impacto."""
        self._require_base(amount_in)
        return TokenAmount(
            raw=self._output_raw(amount_in.raw),
            decimals=self.reserve_quote.decimals,
            symbol=self.reserve_quote.symbol,
        )

    def price_impact(self, amount_in: TokenAmount) -> BasisPoints:
        """Cuánto empeora el precio por el tamaño de la orden, sin contar comisión.

        Siempre >= 0: en un pool de producto constante, vender mueve el precio
        en tu contra.
        """
        gross_out = self.output_without_fee(amount_in)
        if gross_out.is_zero:
            return BasisPoints(0)
        executed = amount_in.price_against(gross_out)
        mid = self.mid_price
        shortfall = EXACT.divide(EXACT.subtract(mid.value, executed.value), mid.value)
        return abs(BasisPoints.from_ratio(shortfall))

    def has_depth_for(self, amount_in: TokenAmount, *, max_impact: BasisPoints) -> bool:
        """Si el pool aguanta esa orden sin pasarse del impacto máximo.

        Lo usan los motores para **omitir** un venue en vez de devolver una
        cotización catastrófica que ensuciaría la comparación.
        """
        self._require_base(amount_in)
        return self.price_impact(amount_in).value <= max_impact.value


def pool_from_mid_price(
    *,
    base: TokenAmount,
    mid_price: Decimal,
    fee_bps: BasisPoints,
    quote_decimals: int,
    quote_symbol: str,
) -> ConstantProductPool:
    """Construye un pool con la profundidad `base` dada y ese precio medio.

    Atajo para datos de demostración y para tests: se expresa la liquidez en
    unidades del token base, que es como se razona sobre profundidad.
    """
    reserve_quote_human = EXACT.multiply(base.as_decimal(), mid_price)
    return ConstantProductPool(
        reserve_base=base,
        reserve_quote=TokenAmount.from_decimal(
            reserve_quote_human,
            quote_decimals,
            quote_symbol,
            rounding=ROUND_DOWN,
        ),
        fee_bps=fee_bps,
    )
