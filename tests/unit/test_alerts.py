"""Centro de alertas: deduplicación, contadores y suscripción."""

from __future__ import annotations

from amigocompora.app.alerts import Alert, AlertCenter, AlertKind, Severity, severity_for_bps
from amigocompora.domain.clock import FrozenClock


def test_publish_creates_alert() -> None:
    center = AlertCenter(clock=FrozenClock())
    alert = center.publish(
        kind=AlertKind.OPPORTUNITY,
        severity=Severity.LOW,
        title="t",
        detail="d",
        dedup_key="k",
    )
    assert alert.count == 1
    assert center.unacknowledged_count == 1


def test_dedup_updates_and_counts() -> None:
    center = AlertCenter(clock=FrozenClock())
    center.publish(
        kind=AlertKind.OPPORTUNITY,
        severity=Severity.LOW,
        title="t",
        detail="d",
        dedup_key="k",
    )
    again = center.publish(
        kind=AlertKind.OPPORTUNITY,
        severity=Severity.LOW,
        title="t2",
        detail="d2",
        dedup_key="k",
    )
    assert again.count == 2
    assert again.title == "t2"
    assert len(center.alerts()) == 1


def test_listeners_notified() -> None:
    center = AlertCenter(clock=FrozenClock())
    seen: list[Alert] = []
    center.subscribe(seen.append)
    center.publish(kind=AlertKind.INFO, severity=Severity.LOW, title="t", detail="d", dedup_key="k")
    assert len(seen) == 1


def test_acknowledge_all() -> None:
    center = AlertCenter(clock=FrozenClock())
    center.publish(kind=AlertKind.INFO, severity=Severity.LOW, title="a", detail="", dedup_key="a")
    center.publish(kind=AlertKind.INFO, severity=Severity.LOW, title="b", detail="", dedup_key="b")
    center.acknowledge_all()
    assert center.unacknowledged_count == 0


def test_severity_thresholds() -> None:
    assert severity_for_bps(5) is Severity.LOW
    assert severity_for_bps(50) is Severity.MEDIUM
    assert severity_for_bps(200) is Severity.HIGH
