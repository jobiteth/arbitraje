"""La barrera de seguridad: qué concede cada modo y qué nunca se concede."""

from __future__ import annotations

import pytest

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.domain.errors import ModeNotPermittedError
from amigocompora.domain.modes import (
    MODE_CAPABILITIES,
    UNIMPLEMENTED_CAPABILITIES,
    Capability,
    OperationMode,
    modes_granting,
)


@pytest.mark.parametrize(
    ("mode", "capability", "allowed"),
    [
        (OperationMode.OBSERVATION, Capability.READ_CHAIN, True),
        (OperationMode.OBSERVATION, Capability.QUERY_AI, True),
        (OperationMode.OBSERVATION, Capability.COMPUTE_ROUTE, False),
        (OperationMode.SIMULATION, Capability.COMPUTE_ROUTE, True),
        (OperationMode.SIMULATION, Capability.PREPARE_TX, False),
        (OperationMode.ASSISTED, Capability.PREPARE_TX, True),
    ],
)
def test_mode_matrix(mode: OperationMode, capability: Capability, allowed: bool) -> None:
    assert mode.grants(capability) is allowed


def test_default_mode_is_least_capable() -> None:
    guard = ModeGuard()
    assert guard.mode is OperationMode.OBSERVATION


def test_sign_and_broadcast_never_allowed() -> None:
    for mode in OperationMode:
        guard = ModeGuard(mode)
        for capability in UNIMPLEMENTED_CAPABILITIES:
            assert guard.allows(capability) is False


def test_require_raises_with_actionable_message() -> None:
    guard = ModeGuard(OperationMode.OBSERVATION)
    with pytest.raises(ModeNotPermittedError):
        guard.require(Capability.COMPUTE_ROUTE)


def test_upgrade_path_points_to_simulation() -> None:
    guard = ModeGuard(OperationMode.OBSERVATION)
    assert guard.upgrade_path(Capability.COMPUTE_ROUTE) is OperationMode.SIMULATION


def test_upgrade_path_none_for_unimplemented() -> None:
    guard = ModeGuard(OperationMode.OBSERVATION)
    assert guard.upgrade_path(Capability.SIGN_TX) is None


def test_mode_listeners_notified() -> None:
    guard = ModeGuard()
    seen: list[OperationMode] = []
    guard.subscribe(seen.append)
    guard.set_mode(OperationMode.ASSISTED)
    assert seen == [OperationMode.ASSISTED]


def test_every_capability_appears_in_table() -> None:
    granted = set().union(*MODE_CAPABILITIES.values())
    assert Capability.READ_CHAIN in granted
    assert Capability.SIGN_TX not in granted


def test_modes_granting_finds_observer_of_read() -> None:
    assert modes_granting(Capability.READ_CHAIN) == frozenset(OperationMode)
