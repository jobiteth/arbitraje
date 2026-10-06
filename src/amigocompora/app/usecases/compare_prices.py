"""Comparar el precio de ejecución de un mismo tamaño en varios venues.

Capacidad requerida: `READ_CHAIN`. Disponible en los tres modos — es la función
base del producto y no tiene efectos.
"""

from __future__ import annotations

from dataclasses import dataclass

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.errors import NoQuotesError
from amigocompora.domain.models import PriceComparison, TradingPair
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount


@dataclass(frozen=True, slots=True)
class ComparePrices:
    """Cotiza `amount_in` en todos los venues del motor DEX activo."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    async def __call__(self, pair: TradingPair, amount_in: TokenAmount) -> PriceComparison:
        await self.gateway.authorize(
            Capability.READ_CHAIN,
            f"Comparar precios de {pair.symbol}",
            details=(f"Tamaño de la orden: {amount_in}",),
        )

        engine = self.registry.active_dex()
        quotes = await engine.quote(pair, amount_in)

        if not quotes:
            raise NoQuotesError(
                f"ningún venue de «{engine.manifest.name}» cotiza {amount_in} "
                f"de {pair.base.symbol}: probablemente no hay liquidez suficiente "
                f"para ese tamaño"
            )

        return PriceComparison(pair=pair, amount_in=amount_in, quotes=tuple(quotes))
