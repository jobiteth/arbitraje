"""Analizar mercados de predicción: probabilidades implícitas y discrepancias.

Capacidad requerida: `READ_CHAIN`.

El valor analítico está en el *overround*: si la suma de probabilidades de los
resultados se aparta de 1, el mercado está cobrando margen (suma > 1) o
dejándose algo sobre la mesa (suma < 1). Lo segundo es la discrepancia que el
spec pide visualizar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.models import MarketOutcome, PredictionMarket
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints


@dataclass(frozen=True, slots=True)
class MarketReport:
    """Lectura derivada de un mercado. Modelo de presentación, no de dominio."""

    market: PredictionMarket
    overround_bps: BasisPoints
    is_coherent: bool
    favourite: MarketOutcome

    @property
    def question(self) -> str:
        return self.market.question

    @property
    def total_percent(self) -> Decimal:
        return self.market.total_implied_probability * Decimal(100)

    @property
    def note(self) -> str:
        """Explicación en una línea de qué significa el overround observado."""
        if self.is_coherent:
            return "Probabilidades coherentes: la suma cuadra con el 100 %."
        if self.overround_bps.value > 0:
            return (
                f"El mercado cobra {self.overround_bps} de margen: "
                f"las probabilidades suman más del 100 %."
            )
        return (
            f"Las probabilidades suman {abs(self.overround_bps)} menos del 100 %. "
            f"Discrepancia a revisar."
        )


@dataclass(frozen=True, slots=True)
class AnalyzePredictionMarkets:
    """Lista mercados del motor activo y calcula su coherencia interna."""

    registry: EngineRegistry
    gateway: ConfirmationGateway

    async def __call__(
        self,
        *,
        limit: int = 20,
        search: str | None = None,
    ) -> tuple[MarketReport, ...]:
        await self.gateway.authorize(
            Capability.READ_CHAIN,
            "Consultar mercados de predicción",
            details=(f"Búsqueda: {search}" if search else "Todos los mercados disponibles",),
        )

        engine = self.registry.active_prediction()
        markets = await engine.markets(limit=limit, search=search)

        reports = [
            MarketReport(
                market=market,
                overround_bps=market.overround_bps,
                is_coherent=market.is_coherent(),
                favourite=market.favourite,
            )
            for market in markets
        ]
        # Lo incoherente primero: es donde está la señal que el usuario busca.
        return tuple(
            sorted(reports, key=lambda report: abs(report.overround_bps).value, reverse=True)
        )
