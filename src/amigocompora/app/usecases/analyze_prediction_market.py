"""Analizar mercados de predicción: probabilidades implícitas y discrepancias.

Capacidad requerida: `READ_CHAIN`.

El valor analítico está en el *overround*: si la suma de probabilidades de los
resultados se aparta de 1, el mercado está cobrando margen (suma > 1) o
dejándose algo sobre la mesa (suma < 1). Lo segundo es la discrepancia que el
spec pide visualizar.

El orden y el filtro por categoría viven aquí como funciones puras porque la
interfaz los vuelve a aplicar sobre lo ya traído: un selector de la lista no
puede salir a la red, y tener el criterio en un solo sitio evita que el mismo
selector ordene de dos maneras distintas según de dónde venga la lista.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.registry import EngineRegistry
from amigocompora.domain.models import (
    MarketOutcome,
    MarketTag,
    PredictionMarket,
    PredictionSort,
)
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


def sort_reports(
    reports: Sequence[MarketReport],
    sort: PredictionSort,
    *,
    closing_within: timedelta | None = None,
) -> tuple[MarketReport, ...]:
    """Ordena los informes según el criterio pedido.

    Es una función suelta y no parte del caso de uso porque la interfaz la
    necesita igual: los selectores de la lista reordenan **en cliente** lo ya
    traído, sin volver a pedir nada, y con un segundo criterio allá serían dos
    órdenes distintos para la misma pantalla.

    Con `closing_within` manda el reloj —lo que antes cierra, primero—
    por encima de cualquier criterio, igual que en el motor: es la vista de lo
    que está terminando. Un mercado sin fecha no debería llegar aquí, y si
    llegara se va al final en vez de reventar la ordenación: la tabla tiene que
    pintarse.

    `VOLUME` devuelve el orden recibido tal cual: es el que la fuente ya puso al
    pedirle lo más negociado, y reordenar por un dato que puede faltar sería
    cambiar un orden real por una aproximación.
    """
    if closing_within is not None:
        return tuple(
            sorted(
                reports,
                key=lambda report: report.market.closes_at
                or datetime.max.replace(tzinfo=UTC),
            )
        )
    if sort is PredictionSort.NEWEST:
        # Las más nuevas primero; sin fecha conocida, al final: no consta que
        # sean nuevas, y ponerlas primeras afirmaría lo que no se sabe.
        return tuple(
            sorted(
                reports,
                key=lambda report: report.market.created_at
                or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        )
    if sort is PredictionSort.TRENDING:
        # Lo que más se mueve ahora. Sin volumen de 24 h, al final.
        return tuple(
            sorted(
                reports,
                key=lambda report: (
                    report.market.volume_24h
                    if report.market.volume_24h is not None
                    else Decimal(-1)
                ),
                reverse=True,
            )
        )
    if sort is PredictionSort.VOLUME:
        return tuple(reports)
    # Lo incoherente primero: es donde está la señal que el usuario busca.
    return tuple(sorted(reports, key=lambda r: abs(r.overround_bps.value), reverse=True))


def filter_reports_by_tag(
    reports: Sequence[MarketReport], tag: MarketTag
) -> tuple[MarketReport, ...]:
    """Los informes que llevan esa etiqueta.

    Sólo se descarta a quien **consta** que no la lleva: un mercado sin etiquetas
    se queda, porque no se puede afirmar que no sea de la categoría. El motor ya
    pidió el filtro a la fuente; esto es la comprobación de este lado, y con la
    misma regla, para que una fuente que ignore el parámetro no cuele nada ajeno
    ni deje fuera lo que sí vino sin etiquetas.
    """
    return tuple(
        report
        for report in reports
        if not report.market.tags
        or any(suya.slug == tag.slug for suya in report.market.tags)
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
        closing_within: timedelta | None = None,
        category: MarketTag | None = None,
        sort: PredictionSort = PredictionSort.COHERENCE,
    ) -> tuple[MarketReport, ...]:
        """Mercados del motor activo, con su coherencia interna.

        Con `closing_within` la lista pasa a ser la de **lo que está terminando**
        y se ordena por el reloj: lo que antes cierra, primero. Es lo contrario
        del criterio por omisión, y es a propósito —cuando se pregunta qué cierra
        pronto, lo incoherente primero sería una lista ordenada por un criterio
        que no es el que se pidió—.

        `sort` y `category` viajan al motor, que los pide a la fuente y los
        vuelve a aplicar; aquí se repite la ordenación del lado del caso de uso
        para que lo que devuelve esta llamada sea lo mismo que pintará la tabla,
        venga de donde venga el motor.
        """
        await self.gateway.authorize(
            Capability.READ_CHAIN,
            "Consultar mercados de predicción",
            details=(
                f"Búsqueda: {search}" if search else "Todos los mercados disponibles",
                *(
                    (f"Sólo los que cierran en {closing_within}",)
                    if closing_within is not None
                    else ()
                ),
                *((f"Categoría: {category.label}",) if category is not None else ()),
                *(
                    (f"Orden: {sort.value}",)
                    if sort is not PredictionSort.COHERENCE
                    else ()
                ),
            ),
        )

        engine = self.registry.active_prediction()
        markets = await engine.markets(
            limit=limit,
            search=search,
            closing_within=closing_within,
            category=category,
            sort=sort,
        )

        reports = [
            MarketReport(
                market=market,
                overround_bps=market.overround_bps,
                is_coherent=market.is_coherent(),
                favourite=market.favourite,
            )
            for market in markets
        ]
        return sort_reports(reports, sort, closing_within=closing_within)
