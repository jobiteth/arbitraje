"""Comparar las rutas que ofrecen todos los motores de puentes activos.

Capacidad requerida: `READ_CHAIN`. Disponible en los tres modos —no tiene
efectos, sólo pregunta—.

Cotiza **todos** los motores que sepan cruzar desde la red de origen, no sólo el
preferido. Es la misma decisión que en `ComparePrices` y por la misma razón:
comparar es justamente la operación en la que varias fuentes valen más que una.
Aquí pesa incluso más, porque un puente no tiene un precio de mercado único que
se pueda consultar aparte —lo que se lleva el puente y lo que tarda son
propiedades de cada proveedor, no de un pool— y una lista de una sola fila no
sería una comparación.

**El orden lo pone `rank_bridges`**, que ordena por lo que cada ruta entrega. No
se ordena por el orden de activación de los motores ni por quién contestó antes:
eso haría que la «mejor» ruta cambiara de una consulta a la siguiente sin que
nada hubiera cambiado en el mercado.

Un motor que falla no detiene la comparación —para eso están los respaldos— pero
tampoco desaparece sin dejar rastro: su id viaja en `failed_engines` y la
interfaz lo dice.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.errors import NoQuotesError
from amigocompora.domain.models import (
    BridgeComparison,
    BridgeQuote,
    BridgeRequest,
    rank_bridges,
)
from amigocompora.domain.modes import Capability
from amigocompora.domain.protocols import CrossChainPlanner

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompareBridges:
    """Cotiza el mismo cruce en todos los motores de puentes activos."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    async def __call__(self, request: BridgeRequest) -> BridgeComparison:
        await self.gateway.authorize(
            Capability.READ_CHAIN,
            f"Comparar rutas para cruzar a {request.destination.chain}",
            details=(
                f"Cruzas: {request.symbol}",
                f"Entregas: {request.amount_in} en {request.origin.chain}",
                "Cotizar no firma ni emite nada.",
            ),
        )

        engines = self.registry.bridge_planners_for(request.origin.chain)
        by_provider: dict[str, BridgeQuote] = {}
        failed: list[str] = []
        first_error: BaseException | None = None

        for engine in engines:
            engine_id = engine.manifest.engine_id
            try:
                found = await engine.quote_bridge(request)
            except Exception as error:
                # Se sigue con los demás. Un proveedor de puentes que se caiga,
                # agote su cuota o cambie de formato no puede dejar al usuario sin
                # ninguna ruta: para eso están los respaldos. Se registra y se
                # dice en la tabla.
                failed.append(engine_id)
                if first_error is None:
                    first_error = error
                _log.warning(
                    "compare_bridges.engine_failed",
                    engine_id=engine_id,
                    pair=request.symbol,
                    origin=request.origin.chain,
                    error=str(error),
                )
                continue
            for quote in found:
                _keep_first(by_provider, quote, from_engine=engine_id)

        if not by_provider:
            _no_routes(
                request,
                engines=engines,
                failed=failed,
                first_error=first_error,
            )

        return BridgeComparison(
            request=request,
            routes=rank_bridges(tuple(by_provider.values())),
            failed_engines=tuple(failed),
        )


def _keep_first(
    by_provider: dict[str, BridgeQuote], quote: BridgeQuote, *, from_engine: str
) -> None:
    """Se queda con la primera ruta de cada proveedor, y registra el desacuerdo.

    Dos motores pueden acabar usando el **mismo** puente: LI.FI enruta a través de
    Across y Relay puede hacer lo mismo, así que las dos filas describirían el
    mismo camino por el mismo protocolo. Dejar las dos no daría más información
    —daría dos cifras casi iguales y la ilusión de poder elegir entre ellas—, y la
    que se eligiera sería la que hubiera contestado antes.

    Se queda la del motor preferido —el primero de la pila, que es el orden en que
    llegan— y **no** la mejor de las dos. Quedarse con la mejor sería elegir, de
    dos observaciones de lo mismo, la cifra que más conviene; el usuario acabaría
    construyendo contra un importe que no es el que hay.
    """
    previous = by_provider.get(quote.provider)
    if previous is None:
        by_provider[quote.provider] = quote
        return
    if previous.amount_out != quote.amount_out:
        # Que dos motores no coincidan midiendo el mismo puente es un dato sobre
        # los motores, no sobre el puente. Con dos filas desaparecería; aquí queda.
        _log.info(
            "compare_bridges.same_provider_disagreement",
            provider=quote.provider,
            pair=quote.pair_label,
            kept_engine=previous.engine_id,
            kept_out=str(previous.amount_out),
            discarded_engine=from_engine,
            discarded_out=str(quote.amount_out),
        )


def _no_routes(
    request: BridgeRequest,
    *,
    engines: tuple[CrossChainPlanner, ...],
    failed: list[str],
    first_error: BaseException | None,
) -> None:
    """Explica por qué no hay ninguna ruta. Siempre lanza.

    Distingue tres casos, porque significan cosas opuestas:

    - **Ningún motor sabe salir de esa red.** No es que no haya ruta: es que no se
      ha preguntado a nadie. Decir aquí «no hay ruta» afirmaría algo del mercado
      que no se ha llegado a mirar, y mandaría al usuario a cambiar el importe
      cuando lo que falta es activar un motor.
    - **Todos fallaron.** Se propaga el primer error, que es el que mejor lo
      explica, en vez de envolverlo: envolverlo perdería el motivo real.
    - **Respondieron y no ofrecen nada.** Entonces sí es una afirmación sobre el
      cruce, y se dice con los motores consultados al lado.
    """
    origin = request.origin.chain
    if not engines:
        raise NoQuotesError(
            f"ningún motor activo sabe cruzar desde «{origin}»: no hay a quién "
            f"preguntar por {request.symbol}. Activa uno en el panel de motores."
        )
    if first_error is not None and len(failed) == len(engines):
        raise first_error

    consulted = ", ".join(engine.manifest.engine_id for engine in engines)
    sin_responder = f" No respondieron: {', '.join(failed)}." if failed else ""
    raise NoQuotesError(
        f"ningún puente ofrece ruta para {request.amount_in} de "
        f"{request.origin.symbol} desde {origin} hacia {request.destination.chain}: "
        f"puede que el importe sea demasiado pequeño para cubrir la comisión del "
        f"cruce, o que no haya liquidez en ese camino. "
        f"Motores consultados: {consulted}.{sin_responder}"
    )
