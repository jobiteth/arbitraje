"""Centro de alertas / notificaciones de la aplicación.

Etapa 5 (y usado por la 4 y 7): las discrepancias detectadas por
`ScanOpportunities` / `AnalyzePredictionMarkets` / `WatchScan` se publican aquí.
La UI se suscribe y decide cómo mostrarlas (banner, bandeja, sonido).

Diseño:

- No conoce Qt ni ningún toolkit: sólo guarda `Alert` y notifica listeners.
- Deduplicación por `dedup_key`: si la misma oportunidad se detecta en dos
  barridos seguidos, no se duplica en la bandeja — se actualiza `last_seen` y
  contador.
- Severidad derivada del diferencial: permite a la UI priorizar sin recalcular.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from amigocompora.domain.clock import Clock, SystemClock

MAX_ALERTS: Final = 200


class AlertKind(StrEnum):
    OPPORTUNITY = "opportunity"
    PREDICTION_DISCREPANCY = "prediction_discrepancy"
    RPC_DEGRADED = "rpc_degraded"
    ENGINE_ERROR = "engine_error"
    INFO = "info"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def severity_for_bps(net_bps: int) -> Severity:
    if net_bps >= 100:
        return Severity.HIGH
    if net_bps >= 30:
        return Severity.MEDIUM
    return Severity.LOW


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    kind: AlertKind
    severity: Severity
    title: str
    detail: str
    dedup_key: str
    created_at: datetime
    last_seen_at: datetime
    count: int = 1
    acknowledged: bool = False


@dataclass(slots=True)
class AlertCenter:
    clock: Clock = field(default_factory=SystemClock)
    _alerts: list[Alert] = field(default_factory=list)
    _listeners: list[Callable[[Alert], None]] = field(default_factory=list)

    def subscribe(self, listener: Callable[[Alert], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _off() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _off

    def alerts(
        self,
        *,
        kind: AlertKind | None = None,
        only_unacknowledged: bool = False,
    ) -> tuple[Alert, ...]:
        result = tuple(self._alerts)
        if kind is not None:
            result = tuple(a for a in result if a.kind == kind)
        if only_unacknowledged:
            result = tuple(a for a in result if not a.acknowledged)
        return result

    def publish(
        self,
        *,
        kind: AlertKind,
        severity: Severity,
        title: str,
        detail: str,
        dedup_key: str,
    ) -> Alert:
        now = self.clock.now()
        # Deduplicación: si ya existe con la misma clave, actualizar.
        for index, existing in enumerate(self._alerts):
            if existing.dedup_key == dedup_key:
                updated = Alert(
                    alert_id=existing.alert_id,
                    kind=kind,
                    severity=severity,
                    title=title,
                    detail=detail,
                    dedup_key=dedup_key,
                    created_at=existing.created_at,
                    last_seen_at=now,
                    count=existing.count + 1,
                    acknowledged=False,
                )
                self._alerts[index] = updated
                # Mover al frente: lo reciente primero.
                self._alerts.insert(0, self._alerts.pop(index))
                self._notify(updated)
                return updated

        alert = Alert(
            alert_id=uuid.uuid4().hex,
            kind=kind,
            severity=severity,
            title=title,
            detail=detail,
            dedup_key=dedup_key,
            created_at=now,
            last_seen_at=now,
        )
        self._alerts.insert(0, alert)
        # Acotar el historial.
        if len(self._alerts) > MAX_ALERTS:
            self._alerts = self._alerts[:MAX_ALERTS]
        self._notify(alert)
        return alert

    def acknowledge(self, alert_id: str) -> None:
        for index, alert in enumerate(self._alerts):
            if alert.alert_id == alert_id:
                self._alerts[index] = Alert(
                    alert_id=alert.alert_id,
                    kind=alert.kind,
                    severity=alert.severity,
                    title=alert.title,
                    detail=alert.detail,
                    dedup_key=alert.dedup_key,
                    created_at=alert.created_at,
                    last_seen_at=alert.last_seen_at,
                    count=alert.count,
                    acknowledged=True,
                )
                break

    def acknowledge_all(self) -> None:
        for index, alert in enumerate(self._alerts):
            if not alert.acknowledged:
                self._alerts[index] = Alert(
                    alert_id=alert.alert_id,
                    kind=alert.kind,
                    severity=alert.severity,
                    title=alert.title,
                    detail=alert.detail,
                    dedup_key=alert.dedup_key,
                    created_at=alert.created_at,
                    last_seen_at=alert.last_seen_at,
                    count=alert.count,
                    acknowledged=True,
                )

    def clear(self) -> None:
        self._alerts.clear()

    @property
    def unacknowledged_count(self) -> int:
        return sum(1 for a in self._alerts if not a.acknowledged)

    def _notify(self, alert: Alert) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(alert)
            except Exception:
                # Un listener roto no debe perder la alerta.
                import structlog as _sl

                _sl.get_logger(__name__).exception("alert.listener_failed")
