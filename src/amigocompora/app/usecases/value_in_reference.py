"""Cuánto vale en la moneda del tope lo que se va a entregar.

### Por qué existe

Los topes —«5 000 por operación», «20 000 al día»— están escritos en la moneda de
referencia de la red, que es la stablecoin con la que se cotiza. Mientras se
opere con pares que la tocan, el importe entregado **es** el importe del tope y
no hay nada que valorar. Pero en cuanto se opera con un par que no la toca —ETH
contra un token, o dos tokens entre sí— el importe entregado está en otra unidad,
y compararlo tal cual contra el tope sería comparar peras con manzanas: un
`max_quote_per_trade` de 1 000 dejaría pasar sin pestañear una operación de medio
ETH, porque `0,5 < 1 000`.

### Qué hace y qué no

Cotiza lo entregado contra la moneda de referencia con los motores que ya están
activos —los mismos que cotizan el par que el usuario está mirando— y devuelve el
mejor importe de salida. **No inventa ningún precio**: es una cotización más,
medida contra el estado del pool en este bloque, igual que la que se enseña en la
tabla. Un oráculo propio o una tabla de precios escrita en el código darían una
cifra con aspecto de dato y sin nada detrás.

Si ningún motor puede valorarlo, devuelve `None` — y quien llama **no ejecuta**.
Es la decisión importante de este módulo: un tope que se salta cuando no se puede
medir no es un tope, es una sugerencia, y la operación que no se sabe medir es
justo la que más conviene no dejar pasar sin medir.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.models import Token, TradingPair
from amigocompora.domain.money import TokenAmount
from amigocompora.domain.protocols import DexQuoteEngine

_log = structlog.get_logger(__name__)


class ReferenceValuation(Protocol):
    """Saber cuánto vale lo entregado en la moneda en la que se escriben los topes.

    Es un contrato y no la clase de abajo porque quien ejecuta no necesita saber
    **cómo** se valora —cotizando, o como sea—, sólo que hay algo que responde a
    esa pregunta y que sabe explicar cuándo no puede. Es la misma forma que tiene
    `PrivateKeySource`: el ejecutor depende de la pregunta, no del proveedor.
    """

    async def __call__(
        self, spent: Token, amount: TokenAmount, reference: Token
    ) -> TokenAmount | None: ...

    def describe_failure(self, spent: Token, reference: Token) -> str: ...


@dataclass(frozen=True, slots=True)
class ValueInReference:
    """Valora un importe en la moneda de referencia de su red, o no lo valora."""

    registry: EngineRegistry

    async def __call__(
        self, spent: Token, amount: TokenAmount, reference: Token
    ) -> TokenAmount | None:
        """El valor de `amount` (de `spent`) en `reference`, o `None` si no se mide.

        El par se arma con lo entregado como `base` porque es la unidad en la que
        viene el importe: `amount_in` se expresa siempre en la pata `base`. Dar la
        vuelta al par daría la cotización en el sentido contrario, que es el
        precio al que se vende la referencia, no el valor de lo que se entrega.

        El token se recibe aparte del importe porque `TokenAmount` es sólo
        símbolo, escala y entero —no lleva dirección ni red—, y sin la dirección
        no hay pool al que preguntar.
        """
        if spent.symbol == reference.symbol:
            # Medir en sí misma la moneda del tope no es una operación: si el
            # importe ya está en esa moneda, quien llama no necesita preguntar.
            return TokenAmount(amount.raw, reference.decimals, reference.symbol)

        pair = TradingPair(base=spent, quote=reference)
        engines: tuple[DexQuoteEngine, ...] = self.registry.active_dex_stack()
        best: TokenAmount | None = None
        fallos: list[str] = []
        for engine in engines:
            try:
                found = await engine.quote(pair, amount)
            except Exception as error:
                # Una fuente caída no puede impedir valorar si otra puede: es la
                # misma regla que en la comparación de precios, y por el mismo
                # motivo —para eso están los respaldos.
                fallos.append(engine.manifest.engine_id)
                _log.warning(
                    "valuation.engine_failed",
                    engine_id=engine.manifest.engine_id,
                    pair=pair.symbol,
                    error=str(error),
                )
                continue
            for quote in found:
                if best is None or quote.amount_out.raw > best.raw:
                    best = quote.amount_out

        if best is None:
            _log.info(
                "valuation.unavailable",
                pair=pair.symbol,
                amount=str(amount),
                engines=len(engines),
                failed=fallos,
            )
        return best

    def describe_failure(self, spent: Token, reference: Token) -> str:
        """Por qué no se pudo valorar, en una frase que dice qué hacer.

        Se escribe aquí, junto a la búsqueda, para que el mensaje no pueda
        desfasarse de lo que de verdad se intentó.
        """
        return (
            f"no se pudo valorar {spent.symbol} en {reference.symbol} para comprobar "
            f"el tope: ningún motor activo cotiza {spent.symbol} contra "
            f"{reference.symbol} en {spent.chain}. Ejecutar sin poder aplicar el tope "
            f"sería operar sin límite, así que no se ejecuta: añade un motor que "
            f"cubra ese par —o una ruta hasta {reference.symbol}— y vuelve a "
            f"intentarlo."
        )
