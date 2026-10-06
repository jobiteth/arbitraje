"""Scheduler: respeto a los modos, no solape y ciclo de vida."""

from __future__ import annotations

import asyncio

import pytest

from amigocompora.app.mode_guard import ModeGuard
from amigocompora.app.scheduler import ScheduledTask, Scheduler, TaskStatus
from amigocompora.domain.modes import Capability, OperationMode


def _task(task_id: str, capability: Capability, calls: list[int]) -> ScheduledTask:
    async def job() -> None:
        calls.append(1)

    return ScheduledTask(
        task_id=task_id,
        name=task_id,
        capability=capability,
        factory=job,
        interval_seconds=0.02,
    )


async def test_disabled_by_mode_is_skipped() -> None:
    calls: list[int] = []
    scheduler = Scheduler(ModeGuard(OperationMode.OBSERVATION))
    task = scheduler.register(_task("t", Capability.COMPUTE_ROUTE, calls))
    await scheduler.trigger_now("t")
    assert calls == []
    assert task.stats.skipped_mode == 1


async def test_allowed_capability_runs() -> None:
    calls: list[int] = []
    scheduler = Scheduler(ModeGuard(OperationMode.SIMULATION))
    scheduler.register(_task("t", Capability.COMPUTE_ROUTE, calls))
    await scheduler.trigger_now("t")
    assert calls == [1]


async def test_loop_runs_and_stops() -> None:
    calls: list[int] = []
    scheduler = Scheduler(ModeGuard(OperationMode.SIMULATION))
    scheduler.register(_task("t", Capability.READ_CHAIN, calls))
    scheduler.start()
    await asyncio.sleep(0.15)
    await scheduler.stop()
    assert len(calls) >= 2
    assert not scheduler.is_running


async def test_overlap_is_skipped() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()

    scheduler = Scheduler(ModeGuard(OperationMode.SIMULATION))
    scheduler.register(
        ScheduledTask(
            task_id="slow",
            name="slow",
            capability=Capability.READ_CHAIN,
            factory=slow,
            interval_seconds=100,
        )
    )
    first = asyncio.create_task(scheduler.trigger_now("slow"))
    await started.wait()
    await scheduler.trigger_now("slow")  # debe omitirse por solape
    release.set()
    await first
    task = scheduler.task("slow")
    assert task.stats.skipped_overlap == 1


async def test_failure_recorded_and_recovers() -> None:
    attempts = {"n": 0}

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")

    scheduler = Scheduler(ModeGuard(OperationMode.SIMULATION))
    task = scheduler.register(
        ScheduledTask(
            task_id="flaky",
            name="flaky",
            capability=Capability.READ_CHAIN,
            factory=flaky,
            interval_seconds=100,
        )
    )
    await scheduler.trigger_now("flaky")
    assert task.stats.failures == 1
    await scheduler.trigger_now("flaky")
    assert task.stats.runs == 1
    assert task.status is TaskStatus.IDLE


async def test_duplicate_registration_rejected() -> None:
    scheduler = Scheduler(ModeGuard())
    scheduler.register(_task("t", Capability.READ_CHAIN, []))
    with pytest.raises(ValueError, match="ya registrada"):
        scheduler.register(_task("t", Capability.READ_CHAIN, []))
