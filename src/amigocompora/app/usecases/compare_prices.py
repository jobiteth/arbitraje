"""Comparar el precio de ejecución de un mismo tamaño en varios venues.

Capacidad requerida: `READ_CHAIN`. Disponible en los tres modos — es la función
base del producto y no tiene efectos.

Cotiza **todos** los motores activos de la ranura, no sólo el preferido.
Comparar es justamente la operación en la que varias fuentes valen más que una,
y es lo que hace que dos agregados como 0x y Uniswap puedan verse frente a
frente en la misma tabla en vez de tener que elegir uno y perder al otro.

Un motor que falla no detiene la comparación —ésa es la razón de tener
respaldos— pero tampoco desaparece sin dejar rastro: su id viaja en
`failed_engines` y la interfaz lo dice. Ver `PriceComparison`.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.errors import NoQuotesError
from amigocompora.domain.models import PriceComparison, Quote, TradingPair
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import DexQuoteEngine

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComparePrices:
    """Cotiza `amount_in` en todos los venues de los motores DEX activos."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    async def __call__(self, pair: TradingPair, amount_in: TokenAmount) -> PriceComparison:
        await self.gateway.authorize(
            Capability.READ_CHAIN,
            f"Comparar precios de {pair.symbol}",
            details=(f"Tamaño de la orden: {amount_in}",),
        )

        engines = self.registry.active_dex_stack()
        by_venue: dict[str, Quote] = {}
        failed: list[str] = []
        first_error: BaseException | None = None

        for engine in engines:
            engine_id = engine.manifest.engine_id
            try:
                found = await engine.quote(pair, amount_in)
            except Exception as error:
                # Se sigue con los demás. Que una fuente se caiga, agote su cuota
                # o cambie de formato no puede dejar al usuario sin comparación:
                # para eso están los respaldos. Se registra y se dice.
                failed.append(engine_id)
                if first_error is None:
                    first_error = error
                _log.warning(
                    "compare.engine_failed",
                    engine_id=engine_id,
                    pair=pair.symbol,
                    chain=pair.chain,
                    error=str(error),
                )
                continue
            for quote in found:
                _keep_first(by_venue, quote, from_engine=engine_id)

        if not by_venue:
            _no_quotes(pair, amount_in, engines=engines, failed=failed, first_error=first_error)

        return PriceComparison(
            pair=pair,
            amount_in=amount_in,
            quotes=tuple(by_venue.values()),
            failed_engines=tuple(failed),
        )


def _keep_first(by_venue: dict[str, Quote], quote: Quote, *, from_engine: str) -> None:
    """Se queda con la primera medición de cada venue, y registra el desacuerdo.

    Dos motores pueden medir **el mismo** sitio: el `venue_id` identifica el
    protocolo y la red, no la fuente, así que GeckoTerminal y DexScreener mirando
    el mismo pool de Uniswap V3 producen el mismo identificador. Dejar las dos no
    daría más información: daría un `spread_bps` que es la diferencia entre dos
    mediciones del mismo contrato, o sea ruido con aspecto de oportunidad.

    Se queda la del motor preferido —el primero de la pila, que es el orden en
    que llegan— y **no** la mejor de las dos. Quedarse con la mejor sería elegir,
    de dos observaciones del mismo pool, la cifra que más conviene; el usuario
    construiría contra un precio que no es el que hay.
    """
    previous = by_venue.get(quote.venue.venue_id)
    if previous is None:
        by_venue[quote.venue.venue_id] = quote
        return
    if previous.amount_out != quote.amount_out:
        # Que dos fuentes no coincidan midiendo lo mismo es un dato sobre las
        # fuentes, no sobre el mercado. Con dos filas desaparecería; aquí queda.
        _log.info(
            "compare.same_venue_disagreement",
            venue=quote.venue.venue_id,
            pair=quote.pair.symbol,
            kept_engine=previous.engine_id,
            kept_out=str(previous.amount_out),
            discarded_engine=from_engine,
            discarded_out=str(quote.amount_out),
        )


def _no_quotes(
    pair: TradingPair,
    amount_in: TokenAmount,
    *,
    engines: tuple[DexQuoteEngine, ...],
    failed: list[str],
    first_error: BaseException | None,
) -> None:
    """Explica por qué no hay nada que comparar. Siempre lanza.

    Distingue los dos casos, porque significan cosas opuestas: que **ninguna**
    fuente haya podido responder es un problema de acceso, y decir ahí «no hay
    liquidez» afirmaría algo del mercado que no se ha llegado a mirar.
    """
    if first_error is not None and len(failed) == len(engines):
        # Se propaga el fallo de la preferida, que es el que mejor lo explica, en
        # vez de envolverlo: envolverlo perdería el motivo real.
        raise first_error

    consulted = ", ".join(engine.manifest.engine_id for engine in engines)
    sin_responder = f" No respondieron: {', '.join(failed)}." if failed else ""
    raise NoQuotesError(
        f"ningún venue cotiza {amount_in} de {pair.base.symbol} en {pair.chain}: "
        f"probablemente no hay liquidez suficiente para ese tamaño. "
        f"Motores consultados: {consulted}.{sin_responder}"
    )
