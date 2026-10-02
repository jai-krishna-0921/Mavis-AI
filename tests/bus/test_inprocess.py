import asyncio
from datetime import UTC, datetime

from mavis.bus.inprocess import InProcessBus
from mavis.domain.events import Event, EventType, Job, JobKind


def ev(event_id: str, user_id: int = 1) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=datetime.now(UTC), source="test")


async def test_duplicate_event_processed_once() -> None:
    bus = InProcessBus()
    seen: list[str] = []

    async def handler(e: Event) -> None:
        seen.append(e.id)

    assert await bus.publish(ev("tg:update:1"))
    assert not await bus.publish(ev("tg:update:1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert seen == ["tg:update:1"]


async def test_failing_event_retried_then_dead_lettered() -> None:
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert calls == InProcessBus.MAX_ATTEMPTS
    assert [e.id for e in bus.dead_events] == ["e1"]


async def test_event_succeeds_on_retry() -> None:
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("flaky")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert calls == 3 and bus.dead_events == []


async def test_jobs_delivered_and_attempts_incremented_on_retry() -> None:
    bus = InProcessBus()
    attempts: list[int] = []

    async def handler(j: Job) -> None:
        attempts.append(j.attempts)
        if j.attempts == 0:
            raise RuntimeError("first try fails")

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN))
    task = asyncio.create_task(bus.consume_jobs("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert attempts == [0, 1]


async def test_wait_idle_waits_for_event_published_by_a_running_job() -> None:
    """Regression: an event published by a job and still being handled must not count as idle."""
    bus = InProcessBus()
    done: list[str] = []

    async def on_job(j: Job) -> None:
        await bus.publish(ev("from-job"))
        await asyncio.sleep(0.02)  # job ends while the event is dequeued and still in flight

    async def on_event(e: Event) -> None:
        await asyncio.sleep(0.2)
        done.append(e.id)

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN))
    tasks = [asyncio.create_task(bus.consume_jobs("g", "c", on_job)),
             asyncio.create_task(bus.consume_events("g", "c", on_event))]
    await bus.wait_idle()
    for t in tasks:
        t.cancel()
    assert done == ["from-job"]
