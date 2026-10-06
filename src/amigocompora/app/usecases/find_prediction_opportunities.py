"""Oportunidades de compra en mercados de predicción.

Capacidad requerida: `READ_CHAIN`. Es un hallazgo analítico de sólo lectura: se
calcula sobre precios ya publicados y no propone ninguna orden.

### La condición de arbitraje, y por qué es exacta

En un mercado de predicción los resultados son **mutuamente excluyentes y
exhaustivos**: ocurre exactamente uno. Si se compra una participación de *cada*
resultado, la cesta paga exactamente 1 sin importar cuál de ellos ocurra. Por
tanto:

    coste de la cesta = suma de los precios de todos los resultados

y si esa suma es menor que 1, comprar el mercado entero devuelve más de lo que
costó. La ganancia es `1 - coste`, y en puntos básicos es exactamente el
`overround_bps` del dominio cambiado de signo: `overround = coste - 1`, así que
un overround **negativo** es la oportunidad.

Es la única señal de este tipo que se puede afirmar con los datos publicados sin
traer un modelo de valoración externo. Decir «esta probabilidad está mal» exige
una opinión sobre el futuro; decir «esta cesta cuesta menos de lo que paga» no
exige ninguna.

### Lo que este análisis **no** incluye, y hay que decirlo

- **Gas y comisiones de la plataforma.** El margen calculado es bruto. En Polygon
  el gas es bajo, pero no es cero, y un margen de 60 bps sobre una cesta pequeña
  se lo puede comer entero.
- **Profundidad real.** Que el precio publicado sea 0,40 no garantiza que se
  puedan comprar participaciones a ese precio: el libro puede sostener mucho
  menos de lo que se necesita para que la operación valga la pena.
- **Simultaneidad.** Las cuatro patas hay que ejecutarlas a la vez. Si el precio
  se mueve entre la primera y la última, la cesta que se compró no es la que se
  calculó. Es el riesgo clásico de este arbitraje y no se puede medir desde aquí.

Por eso el resultado se presenta como una **desviación a revisar**, con su
tamaño, y no como una oportunidad «accionable» —a diferencia de
`scan_opportunities`, donde el diferencial sí se puede comparar contra comisiones
conocidas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.usecases.analyze_prediction_market import (
    AnalyzePredictionMarkets,
    MarketReport,
)
from amigocompora.domain.clock import Clock
from amigocompora.domain.models import MarketOutcome, PredictionMarket
from amigocompora.domain.money import EXACT, BasisPoints

#: Margen mínimo para reportar la desviación. 50 bps = 0,5 %.
#:
#: No es cero a propósito. Polymarket publica precios complementarios, así que
#: las desviaciones de uno o dos bps aparecen constantemente por redondeo y son
#: ruido, no señal: el mínimo de la plataforma es 1 ¢ sobre 100, es decir 100 bps
#: por resultado, así que por debajo de eso no hay nada que capturar.
DEFAULT_MIN_EDGE_BPS: Final = BasisPoints(50)

_ONE: Final = Decimal(1)


@dataclass(frozen=True, slots=True)
class BasketOpportunity:
    """Comprar todos los resultados cuesta menos que el pago garantizado de 1.

    Modelo de presentación calculado sobre un `PredictionMarket`, no una
    instrucción. Ver el docstring del módulo para lo que queda fuera.
    """

    market: PredictionMarket
    #: Suma de los precios de todos los resultados = lo que cuesta la cesta.
    cost: Decimal
    #: `1 - cost`, por cada unidad de pago garantizado. Siempre positivo aquí.
    profit_per_share: Decimal
    #: Margen sobre el capital invertido: `profit / cost`.
    return_on_cost: Decimal
    observed_at: datetime

    @property
    def question(self) -> str:
        return self.market.question

    @property
    def outcomes(self) -> tuple[MarketOutcome, ...]:
        return self.market.outcomes

    @property
    def discount_bps(self) -> BasisPoints:
        """Cuánto más barata es la cesta que el pago garantizado de 1.

        Es exactamente `-overround_bps` del mercado, porque el overround es
        `coste - 1`. Es la cifra que se compara contra el umbral: mide el margen
        en la misma escala en que la fuente publica las desviaciones.
        """
        return BasisPoints.from_ratio(self.profit_per_share)

    @property
    def return_on_cost_bps(self) -> BasisPoints:
        """Margen sobre el capital realmente invertido: `profit / cost`.

        Es mayor que `discount_bps` y no lo sustituye: sobre una cesta de 0,98
        el descuento es 200 bps pero el retorno es 204 bps, porque el capital
        desembolsado es 0,98 y no 1. Cuál de los dos importa depende de si el
        coste de oportunidad se mide contra el pago o contra el desembolso, así
        que se publican los dos en vez de elegir uno por el usuario.
        """
        return BasisPoints.from_ratio(self.return_on_cost)

    @property
    def outcome_count(self) -> int:
        return len(self.market.outcomes)

    @property
    def note(self) -> str:
        return (
            f"Comprar los {self.outcome_count} resultados cuesta "
            f"{self.cost:.4f} y paga 1,0000: margen bruto del "
            f"{(self.profit_per_share * 100):.2f} % sobre el pago, "
            f"{(self.return_on_cost * 100):.2f} % sobre el capital."
        )


@dataclass(frozen=True, slots=True)
class FindPredictionOpportunities:
    """Deriva cestas con margen de los mercados ya leídos."""

    analyze: AnalyzePredictionMarkets
    gateway: ConfirmationGateway
    clock: Clock

    async def __call__(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
        min_edge_bps: BasisPoints = DEFAULT_MIN_EDGE_BPS,
    ) -> tuple[BasketOpportunity, ...]:
        reports = await self.analyze(limit=limit, search=search)
        return self.from_reports(reports, min_edge_bps=min_edge_bps)

    def from_reports(
        self,
        reports: tuple[MarketReport, ...],
        *,
        min_edge_bps: BasisPoints = DEFAULT_MIN_EDGE_BPS,
    ) -> tuple[BasketOpportunity, ...]:
        """Deriva las cestas de unos informes ya obtenidos.

        Separado de `__call__` para poder testear la aritmética sin motores ni
        red, y para que la UI pueda reanalizar con otro umbral sin volver a
        consultar la fuente.
        """
        observed_at = self.clock.now()
        found: list[BasketOpportunity] = []
        for report in reports:
            cost = report.market.total_implied_probability
            profit = EXACT.subtract(_ONE, cost)
            # `profit > 0` ya lo garantiza el umbral, que es estrictamente
            # positivo; se comprueba igualmente para que la división no pueda
            # recibir un coste no positivo si alguien pasa un umbral de 0.
            if profit <= 0 or cost <= 0:
                continue
            opportunity = BasketOpportunity(
                market=report.market,
                cost=cost,
                profit_per_share=profit,
                return_on_cost=EXACT.divide(profit, cost),
                observed_at=observed_at,
            )
            if opportunity.discount_bps.value >= min_edge_bps.value:
                found.append(opportunity)

        # Mayor margen primero: es el orden en el que el usuario quiere verlas.
        return tuple(sorted(found, key=lambda opp: opp.discount_bps.value, reverse=True))
