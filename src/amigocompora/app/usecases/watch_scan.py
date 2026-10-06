"""Caso de uso: escaneo periódico de oportunidades para pares vigilados.

Complementa a `ScanOpportunities` (que es bajo demanda) con una versión
que el scheduler puede invocar cada N segundos sobre una lista fija de pares.
Publica sus hallazgos a través de un callback — la UI o el centro de
notificaciones se suscribe sin que este módulo conozca Qt.

Etapa 4: modo `SIMULACIÓN` mínimo, `OBSERVACIÓN` lo bloquea.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

from amigocompora.app.confirmation import ConfirmationGateway
from amigocompora.app.usecases.scan_opportunities import ScanOpportunities
from amigocompora.domain.models import Opportunity, TradingPair
from amigocompora.domain.modes import Capability
from amigocompora.domain.money import BasisPoints, TokenAmount

_log = structlog.get_logger(__name__)

OpportunityListener = Callable[[tuple[Opportunity, ...]], None]


@dataclass(slots=True)
class WatchedPair:
    pair: TradingPair
    amount_in: TokenAmount
    min_net_bps: BasisPoints


@dataclass(slots=True)
class WatchScan:
    """Escanea una lista de pares vigilados y notifica oportunidades nuevas."""

    scan: ScanOpportunities
    gateway: ConfirmationGateway
    pairs: list[WatchedPair] = field(default_factory=list)
    _listeners: list[OpportunityListener] = field(default_factory=list)

    def set_pairs(self, pairs: Sequence[WatchedPair]) -> None:
        self.pairs = list(pairs)

    def subscribe(self, listener: OpportunityListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _off() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _off

    async def run_once(self) -> tuple[Opportunity, ...]:
        """Un barrido completo. Respeta la barrera de modos."""
        # Autorización temprana: si el modo no permite, no se gasta red.
        await self.gateway.authorize(
            Capability.COMPUTE_ROUTE,
            "Barrido de pares vigilados",
            details=(f"{len(self.pairs)} par(es) vigilado(s)",),
        )
        all_found: list[Opportunity] = []
        for watched in self.pairs:
            try:
                found = await self.scan(
                    watched.pair,
                    watched.amount_in,
                    min_net_bps=watched.min_net_bps,
                )
            except Exception as error:
                _log.warning(
                    "watch_scan.pair_failed",
                    pair=watched.pair.symbol,
                    reason=str(error),
                )
                continue
            all_found.extend(found)
        # Orden global por diferencial neto descendente.
        all_found.sort(key=lambda o: o.net_spread_bps.value, reverse=True)
        result = tuple(all_found)
        for listener in tuple(self._listeners):
            try:
                listener(result)
            except Exception:
                _log.exception("watch_scan.listener_failed")
        return result
