"""Detectar discrepancias de precio entre venues para un mismo tamaño.

Capacidad requerida: `COMPUTE_ROUTE`. **No** está disponible en `OBSERVACIÓN`:
calcular diferenciales netos ya es análisis operativo, no simple consulta, y el
spec reserva ese modo a la lectura.

El resultado es un hallazgo informativo. No hay ninguna ruta de código que lo
convierta en una orden: lo que sigue después lo decide el usuario.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.usecases.compare_prices import ComparePrices
from amigocompora.domain.clock import Clock
from amigocompora.domain.models import Opportunity, PriceComparison, TradingPair
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount

_log = structlog.get_logger(__name__)

#: Umbral por defecto: sólo se reportan diferenciales que sobrevivan a las
#: comisiones con algo de margen. 10 bps = 0,1 %. Por debajo, el ruido de
#: actualización de precios domina sobre la señal.
DEFAULT_MIN_NET_BPS: BasisPoints = BasisPoints(10)


@dataclass(frozen=True, slots=True)
class ScanOpportunities:
    """Cruza la mejor ejecución contra el resto de venues del par."""

    compare_prices: ComparePrices
    gateway: ConfirmationGateway
    clock: Clock

    async def __call__(
        self,
        pair: TradingPair,
        amount_in: TokenAmount,
        *,
        min_net_bps: BasisPoints = DEFAULT_MIN_NET_BPS,
    ) -> tuple[Opportunity, ...]:
        # Se autoriza el cálculo antes de pedir los datos: si el modo no lo
        # permite, no se gastan peticiones de red para nada.
        await self.gateway.authorize(
            Capability.COMPUTE_ROUTE,
            f"Analizar discrepancias en {pair.symbol}",
            details=(
                f"Tamaño de la orden: {amount_in}",
                f"Umbral neto mínimo: {min_net_bps}",
            ),
        )
        comparison = await self.compare_prices(pair, amount_in)
        return self.from_comparison(comparison, min_net_bps=min_net_bps)

    def from_comparison(
        self,
        comparison: PriceComparison,
        *,
        min_net_bps: BasisPoints = DEFAULT_MIN_NET_BPS,
    ) -> tuple[Opportunity, ...]:
        """Deriva las oportunidades de una comparación ya obtenida.

        Separado de `__call__` para que la UI pueda reanalizar con otro umbral
        sin volver a cotizar, y para poder testear la aritmética sin motores.

        **Una cotización sin comisión desglosada no entra en el análisis.** Hay
        fuentes que dan el `amount_out` neto exacto sin decir cuánta comisión
        lleva dentro (`Quote.fee_bps is None`; el caso medido es Jupiter en
        Solana). Ese dato sirve para comparar precios —de hecho es el precio
        ejecutable— pero no para calcular un diferencial **neto**, que es una
        resta: tratar la comisión desconocida como cero daría un
        `net_spread_bps` optimista y un `is_actionable` que promete margen donde
        podría no haberlo. Se omite y se dice, en vez de rellenar el hueco.
        """
        priced = tuple(quote for quote in comparison.quotes if quote.fee_bps is not None)
        if len(priced) != len(comparison.quotes):
            _log.info(
                "opportunities.quotes_without_fee_skipped",
                pair=comparison.pair.symbol,
                skipped=[
                    quote.venue.venue_id for quote in comparison.quotes if quote.fee_bps is None
                ],
            )
        # `best` se recalcula sobre las que sí tienen comisión: la mejor
        # ejecución global puede ser justo una de las descartadas, y usarla como
        # referencia sin poder restarle su comisión reintroduciría el problema.
        best = max(priced, key=lambda quote: quote.amount_out.raw, default=None)
        if best is None or best.fee_bps is None:
            return ()

        observed_at = self.clock.now()
        opportunities = [
            Opportunity(
                pair=comparison.pair,
                amount_in=comparison.amount_in,
                best=best,
                reference=reference,
                # Ambos lados cobran comisión: entrar en uno y salir del otro.
                total_fee_bps=BasisPoints(best.fee_bps.value + fee.value),
                observed_at=observed_at,
            )
            for reference in priced
            if reference.venue.venue_id != best.venue.venue_id
            and (fee := reference.fee_bps) is not None
        ]
        return tuple(
            sorted(
                (
                    opportunity
                    for opportunity in opportunities
                    if opportunity.net_spread_bps.value >= min_net_bps.value
                ),
                key=lambda opportunity: opportunity.net_spread_bps.value,
                reverse=True,
            )
        )
