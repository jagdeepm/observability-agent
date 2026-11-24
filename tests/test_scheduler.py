"""Tests for the Scheduler — queue management, concurrency, retry."""

from __future__ import annotations

import asyncio

import pytest

from observability_agent.infrastructure.repository import InMemoryIncidentRepository
from observability_agent.infrastructure.scheduler import Scheduler


class MockCoordinator:
    """Coordinator that records calls and optionally delays/fails.

    fail=True raises a generic RuntimeError (the retryable case). Pass an
    Exception instance instead to control exactly what's raised — e.g. a
    LLMBudgetExceededError, which the Scheduler must treat differently.
    """

    def __init__(self, delay: float = 0, fail: bool | Exception = False) -> None:
        self.delay = delay
        self.fail = fail
        self.calls: list[str] = []

    async def run(self, incident_id: str) -> None:
        self.calls.append(incident_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is True:
            raise RuntimeError("mock failure")
        if isinstance(self.fail, Exception):
            raise self.fail


@pytest.mark.asyncio
async def test_enqueue_and_process():
    """Incidents are enqueued and processed by the coordinator."""
    repo = InMemoryIncidentRepository()
    coord = MockCoordinator()
    sched = Scheduler(coordinator=coord, repo=repo, max_concurrent=2)

    sched.enqueue("inc-1")
    sched.enqueue("inc-2")
    sched.enqueue("inc-3")
    assert sched.queue_size == 3

    await sched.start()
    await asyncio.sleep(0.2)
    await sched.stop()

    assert len(coord.calls) == 3
    assert set(coord.calls) == {"inc-1", "inc-2", "inc-3"}


@pytest.mark.asyncio
async def test_critical_jumps_queue():
    """Critical severity incidents jump to the front of the queue."""
    repo = InMemoryIncidentRepository()
    coord = MockCoordinator()
    sched = Scheduler(coordinator=coord, repo=repo)

    sched.enqueue("inc-1", severity="warning")
    sched.enqueue("inc-2", severity="warning")
    sched.enqueue("inc-3", severity="critical")

    # Before starting, check queue order: inc-3 critical jumped to front
    assert sched.queue_position("inc-3") == 0
    assert sched.queue_position("inc-1") == 1


@pytest.mark.asyncio
async def test_concurrency_cap():
    """Respects max_concurrent — only N run at a time."""
    repo = InMemoryIncidentRepository()
    coord = MockCoordinator(delay=0.1)
    sched = Scheduler(coordinator=coord, repo=repo, max_concurrent=2)

    for i in range(5):
        sched.enqueue(f"inc-{i}")

    await sched.start()
    await asyncio.sleep(0.05)
    assert sched.running_count <= 2
    await sched.stop()


@pytest.mark.asyncio
async def test_retry_on_failure():
    """Failed investigations are retried up to max_retries."""
    repo = InMemoryIncidentRepository()
    from observability_agent.domain.models import Incident
    inc = Incident(cluster_id="test", title="test")
    await repo.save(inc)

    coord = MockCoordinator(fail=True)
    sched = Scheduler(coordinator=coord, repo=repo, max_concurrent=1, max_retries=2)

    sched.enqueue(inc.id)
    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()

    # 1 initial + 2 retries = 3 calls
    assert len(coord.calls) == 3


@pytest.mark.asyncio
async def test_budget_exceeded_skips_retry():
    """A budget-exceeded failure is not retried — it fails identically every
    time until the window resets, so retrying only head-of-line-blocks the
    queue behind attempts that are guaranteed to fail the same way."""
    from datetime import datetime, timezone

    from observability_agent.domain.models import Incident
    from observability_agent.infrastructure.llm_budget import LLMBudgetExceededError

    repo = InMemoryIncidentRepository()
    inc = Incident(cluster_id="test", title="test")
    await repo.save(inc)

    err = LLMBudgetExceededError("daily", 500_000, 500_000, datetime.now(timezone.utc))
    coord = MockCoordinator(fail=err)
    sched = Scheduler(coordinator=coord, repo=repo, max_concurrent=1, max_retries=3)

    sched.enqueue(inc.id)
    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()

    assert len(coord.calls) == 1  # no retries — contrast with test_retry_on_failure's 3

    saved = await repo.get(inc.id)
    assert saved.llm_budget_exceeded
    assert not saved.is_stale


@pytest.mark.asyncio
async def test_queue_position_none_when_not_queued():
    """queue_position returns None for non-queued incidents."""
    repo = InMemoryIncidentRepository()
    sched = Scheduler(coordinator=MockCoordinator(), repo=repo)
    assert sched.queue_position("nonexistent") is None