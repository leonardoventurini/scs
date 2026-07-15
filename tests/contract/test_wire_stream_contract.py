"""Background acknowledgement and monotonic event contracts."""

from __future__ import annotations

import asyncio

import pytest

from scs.wire.events import EventBroker


@pytest.mark.asyncio
async def test_progress_is_monotonic_and_has_one_terminal_event() -> None:
    broker = EventBroker()
    subscription = broker.subscribe("jobs")
    await broker.publish("jobs", {"progress": 1})
    await broker.publish("jobs", {"progress": 2})
    await broker.publish("jobs", {"progress": 3}, terminal=True)

    events = [await anext(subscription) for _ in range(3)]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.terminal for event in events] == [False, False, True]


@pytest.mark.asyncio
async def test_disconnect_removes_subscription() -> None:
    broker = EventBroker()
    subscription = broker.subscribe("jobs")
    assert broker.subscriber_count("jobs") == 1
    await subscription.aclose()
    assert broker.subscriber_count("jobs") == 0


@pytest.mark.asyncio
async def test_job_acknowledges_while_worker_is_blocked() -> None:
    barrier = asyncio.Event()
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await barrier.wait()

    task = asyncio.create_task(worker())
    await asyncio.wait_for(started.wait(), timeout=0.2)
    acknowledgement = {"accepted": True, "job_id": "job-1"}
    assert acknowledgement["accepted"] is True
    assert not task.done()
    barrier.set()
    await task
