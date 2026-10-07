import asyncio

import pytest

from mavis.bus.inprocess import InProcessBus
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Job, JobKind


def ev(event_id: str, user_id: int = 1) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="test")


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


@pytest.mark.inline_retries
async def test_inline_retries_recover_without_redelivery(monkeypatch) -> None:
    from mavis.bus import base

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(base, "_sleep", fake_sleep)
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("flaky")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert calls == 3 and slept == [2, 5] and bus.dead_events == []


@pytest.mark.inline_retries
async def test_non_transient_error_skips_inline_retries(monkeypatch) -> None:
    from mavis.bus import base

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(base, "_sleep", fake_sleep)
    bus = InProcessBus()
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("bug")

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("g", "c", handler))
    await bus.wait_idle()
    task.cancel()
    assert slept == [] and calls == InProcessBus.MAX_ATTEMPTS  # normal bus retry path only


async def test_inline_retry_waits_out_llm_backoff(monkeypatch) -> None:
    from mavis.bus import base
    from mavis.domain.errors import LLMError
    from mavis.llm import models

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(base, "_sleep", fake_sleep)
    monkeypatch.setattr(base, "INLINE_RETRY_DELAYS_S", (2,))
    models._ollama.note_rate_limit(None)  # 5s global backoff
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("429")

    await base.run_with_inline_retries(flaky, what="job", ref="x")
    assert calls == 2 and 4 < slept[0] <= 5  # waited for the backoff, not the 2s default
