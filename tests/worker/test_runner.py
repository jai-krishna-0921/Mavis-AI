import asyncio
from datetime import UTC, datetime

import pytest

from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Job, JobKind, Trust
from mavis.store.db import utcnow
from mavis.store.repo import outbox, users
from mavis.worker.runner import (
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


async def test_concurrent_consumers_keep_per_user_order(settings, bus) -> None:
    from tests.fakes import wait_until

    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.05 if event.id == "first" else 0)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    task = asyncio.create_task(run_worker(bus, "w", concurrency=4))
    await bus.publish(ev("first", user_id=1))
    await bus.publish(ev("second", user_id=1))
    await bus.publish(ev("other", user_id=2))
    await wait_until(lambda: len(log) == 6)
    task.cancel()
    assert log.index("end:first") < log.index("start:second")
    assert log.index("start:other") < log.index("end:first")  # different users still overlap


async def test_run_worker_names_consumers_with_suffix(settings) -> None:
    names: list[str] = []

    class Spy:
        async def consume_events(self, group, consumer, handler):
            names.append(consumer)

        async def consume_jobs(self, group, consumer, handler):
            names.append(consumer)

    await run_worker(Spy(), "w", concurrency=3)  # type: ignore[arg-type]
    assert sorted(set(names)) == ["w-0", "w-1", "w-2"] and len(names) == 6


async def test_redis_lock_branch_takes_local_lock_first(settings, monkeypatch) -> None:
    # fakeredis has no Lua (evalsha), so redis-py's Lock can't run on it; use a tiny stand-in client.
    from mavis.worker import locks

    class _Lock:
        def __init__(self, held: set[str], name: str) -> None:
            self.held, self.name = held, name

        async def acquire(self) -> bool:
            assert self.name not in self.held, "redis lock taken twice: local lock was not held first"
            self.held.add(self.name)
            return True

        async def release(self) -> None:
            self.held.discard(self.name)

    class _Client:
        held: set[str] = set()

        def lock(self, name: str, **_kw) -> _Lock:
            return _Lock(self.held, name)

    monkeypatch.setattr(locks, "get_redis", lambda: _Client())
    log: list[str] = []

    async def slow(event: Event) -> None:
        log.append(f"start:{event.id}")
        await asyncio.sleep(0.02)
        log.append(f"end:{event.id}")

    register_event_handler(EventType.USER_MESSAGE, slow)
    await asyncio.gather(handle_event(ev("a")), handle_event(ev("b")))
    assert log == ["start:a", "end:a", "start:b", "end:b"]
    assert _Client.held == set()


@pytest.mark.inline_retries
async def test_inline_retries_send_fallback_once_and_succeed(db, monkeypatch) -> None:
    from mavis.bus import base
    from mavis.bus.inprocess import InProcessBus

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(base, "_sleep", no_sleep)
    user, _ = await users.get_or_create_by_chat(43, "Jai")
    calls = 0

    async def flaky(event: Event) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise LLMError("model timeout")

    register_event_handler(EventType.USER_MESSAGE, flaky)
    bus = InProcessBus()
    await bus.publish(ev("tg:update:10", user_id=user.id))
    task = asyncio.create_task(bus.consume_events("g", "c", handle_event))
    await bus.wait_idle()
    task.cancel()
    assert calls == 3 and bus.dead_events == []
    assert await outbox.texts_with_dedupe_prefix("fallback:tg:update:10") == [FALLBACK_TEXT]
