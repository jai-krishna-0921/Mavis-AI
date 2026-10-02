import asyncio
from datetime import UTC, datetime

import pytest

from zento.domain.errors import LLMError
from zento.domain.events import Event, EventType, Job, JobKind, Trust
from zento.store.db import utcnow
from zento.store.repo import outbox, users
from zento.worker.runner import (
    FALLBACK_TEXT,
    handle_event,
    handle_job,
    register_event_handler,
    register_job_handler,
    run_worker,
)


def ev(event_id: str, user_id: int = 1, trust: Trust = Trust.USER) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=datetime.now(UTC), source="test", payload={"text": "hi"}, trust=trust)


async def test_turns_serialised_per_user(settings) -> None:
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.05)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    await asyncio.gather(handle_event(ev("a", user_id=1)), handle_event(ev("b", user_id=1)))
    assert log == ["start:a", "end:a", "start:b", "end:b"]


async def test_different_users_run_concurrently(settings) -> None:
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.05)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    await asyncio.gather(handle_event(ev("a", user_id=1)), handle_event(ev("b", user_id=2)))
    assert log[:2] == ["start:a", "start:b"]


async def test_llm_failure_sends_fallback_message(db) -> None:
    user, _ = await users.get_or_create_by_chat(42, "Jai")

    async def broken(event: Event) -> None:
        raise LLMError("model timeout")

    register_event_handler(EventType.USER_MESSAGE, broken)
    for _ in range(2):  # the bus retries; the fallback must still be sent only once
        with pytest.raises(LLMError):
            await handle_event(ev("tg:update:9", user_id=user.id))
    rows = await outbox.due(utcnow())
    assert [r.text for r in rows] == [FALLBACK_TEXT]


async def test_llm_failure_on_system_event_sends_nothing(db) -> None:
    user, _ = await users.get_or_create_by_chat(42, "Jai")

    async def broken(event: Event) -> None:
        raise LLMError("x")

    register_event_handler(EventType.USER_MESSAGE, broken)
    with pytest.raises(LLMError):
        await handle_event(ev("sys:1", user_id=user.id, trust=Trust.SYSTEM))
    assert await outbox.due(utcnow()) == []


async def test_replace_and_multiple_handlers(settings) -> None:
    calls: list[str] = []

    async def h1(e: Event) -> None:
        calls.append("h1")

    async def h2(e: Event) -> None:
        calls.append("h2")

    register_event_handler(EventType.USER_MESSAGE, h1)
    register_event_handler(EventType.USER_MESSAGE, h1)  # idempotent registration
    register_event_handler(EventType.USER_MESSAGE, h2)
    await handle_event(ev("x"))
    register_event_handler(EventType.USER_MESSAGE, h2, replace=True)
    await handle_event(ev("y"))
    assert calls == ["h1", "h2", "h2"]


async def test_job_dispatch_and_unknown_kind(settings) -> None:
    got: list[Job] = []

    async def learn(job: Job) -> None:
        got.append(job)

    register_job_handler(JobKind.LEARN, learn)
    await handle_job(Job(id="j", user_id=1, kind=JobKind.LEARN))
    await handle_job(Job(id="k", user_id=1, kind=JobKind.CONSOLIDATE))  # no handler: logged, no error
    assert [j.id for j in got] == ["j"]


async def test_run_worker_consumes_bus(bus) -> None:
    got: list[str] = []

    async def h(e: Event) -> None:
        got.append(e.id)

    register_event_handler(EventType.USER_MESSAGE, h)
    task = asyncio.create_task(run_worker(bus, "w1"))
    await bus.publish(ev("e1"))
    await bus.wait_idle()
    task.cancel()
    assert got == ["e1"]
