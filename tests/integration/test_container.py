"""Integración: el contenedor completo se construye y cablea sin red."""

from __future__ import annotations

import pytest

from amigocompora.app.container import build_container
from amigocompora.domain.errors import ModeNotPermittedError
from amigocompora.domain.models import TradingPair
from amigocompora.domain.modes import Capability, OperationMode
from amigocompora.domain.protocols import EngineKind
from amigocompora.engines.catalog import quote_token, wrapped_native
from amigocompora.infra.config import Settings
from amigocompora.infra.secrets import InMemorySecretStore


def _eth_pair() -> TradingPair:
    base = wrapped_native("ethereum")
    quote = quote_token("ethereum")
    assert base is not None
    assert quote is not None
    return TradingPair(base=base, quote=quote)


async def test_container_builds_with_all_slots() -> None:
    container = await build_container(
        Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        # Las tres ranuras quedan con un motor activo por defecto.
        assert container.registry.active_manifest(EngineKind.DEX_QUOTES) is not None
        assert container.registry.active_manifest(EngineKind.PREDICTION_MARKETS) is not None
        advisor = container.registry.active_manifest(EngineKind.AI_ADVISOR)
        assert advisor is not None
        # El asistente por defecto es el offline.
        assert advisor.engine_id == "stub_advisor"
    finally:
        await container.aclose()


async def test_observation_mode_blocks_route_compute() -> None:
    container = await build_container(
        Settings(mode=OperationMode.OBSERVATION),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        pair = _eth_pair()
        with pytest.raises(ModeNotPermittedError):
            await container.scan_opportunities(pair, pair.base.amount(1))
    finally:
        await container.aclose()


async def test_alert_center_and_scheduler_present() -> None:
    container = await build_container(
        Settings(),
        secret_store=InMemorySecretStore(),
        configure_logs=False,
    )
    try:
        assert container.alert_center.unacknowledged_count == 0
        assert not container.scheduler.is_running
        assert Capability.QUERY_AI in container.guard.mode.capabilities
    finally:
        await container.aclose()
