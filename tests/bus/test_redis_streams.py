import asyncio
from datetime import UTC, datetime

import pytest
from fakeredis import FakeAsyncRedis

from mavis.bus.base import Stream
from mavis.bus.redis_streams import RedisStreamsBus
from mavis.domain.events import Event, EventType, Job, JobKind
from tests.fakes import wait_until


def ev(event_id: str) -> Event:
    return Event(id=event_id, user_id=1, type=EventType.USER_MESSAGE,
                 occurred_at=datetime.now(UTC), source="t")


@pytest.fixture
async def rbus():
    client = FakeAsyncRedis(decode_responses=True)
    # fakeredis' blocking XREADGROUP never actually suspends, so an idle consume loop would starve the
    # event loop; yield after each read the way a real socket wait would.
    real_xreadgroup = client.xreadgroup

    async def xreadgroup(*args, **kwargs):
        result = await real_xreadgroup(*args, **kwargs)
        await asyncio.sleep(0.005)
        return result

    client.xreadgroup = xreadgroup
    bus = RedisStreamsBus(client, claim_idle_ms=0, block_ms=10)
    yield bus, client
    await bus.close()


async def test_publish_dedupes(rbus) -> None:
    bus, client = rbus
    assert await bus.publish(ev("tg:update:7"))
    assert not await bus.publish(ev("tg:update:7"))
    assert await client.xlen(Stream.EVENTS.value) == 1
    assert 0 < await client.ttl("mavis:seen:tg:update:7") <= 7 * 24 * 3600


async def test_publish_releases_dedupe_key_when_xadd_fails(rbus) -> None:
    bus, client = rbus
    real_xadd = client.xadd
    calls = 0

    async def flaky_xadd(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("redis down")
        return await real_xadd(*args, **kwargs)

    client.xadd = flaky_xadd
    with pytest.raises(ConnectionError):
        await bus.publish(ev("tg:update:9"))
    assert await client.exists("mavis:seen:tg:update:9") == 0
    assert await bus.publish(ev("tg:update:9"))
    assert await client.xlen(Stream.EVENTS.value) == 1


async def test_consume_delivers_and_acks(rbus) -> None:
    bus, client = rbus
    got: list[Event] = []

    async def handler(e: Event) -> None:
        got.append(e)

    await bus.publish(ev("e1"))
    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))
    await wait_until(lambda: len(got) == 1)
    await wait_until(lambda: _pending_count(client, Stream.EVENTS.value, "workers"))
    task.cancel()
    assert got[0].id == "e1" and got[0].type is EventType.USER_MESSAGE


async def _pending_count(client, stream: str, group: str) -> bool:
    info = await client.xpending(stream, group)
    return info["pending"] == 0


async def test_failing_message_goes_to_dlq_after_max_attempts(rbus) -> None:
    bus, client = rbus
    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    await bus.publish(ev("bad"))
    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))

    async def dead() -> bool:
        return await client.xlen(f"{Stream.EVENTS.value}:dlq") == 1

    await wait_until(dead, timeout=5)
    task.cancel()
    assert calls == 5


async def test_jobs_round_trip(rbus) -> None:
    bus, _ = rbus
    got: list[Job] = []

    async def handler(j: Job) -> None:
        got.append(j)

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN, payload={"x": 1}))
    task = asyncio.create_task(bus.consume_jobs("workers", "w1", handler))
    await wait_until(lambda: len(got) == 1)
    task.cancel()
    assert got[0].payload == {"x": 1}


def test_get_bus_selects_in_process_without_redis(settings) -> None:
    from mavis.bus import get_bus, get_redis, set_bus
    from mavis.bus.inprocess import InProcessBus

    set_bus(None)
    try:
        assert isinstance(get_bus(), InProcessBus)
        assert get_redis() is None
    finally:
        set_bus(None)


async def test_crashing_worker_message_reaches_dlq_after_max_deliveries(rbus) -> None:
    """Deliveries that never reach the handler's except branch (worker crash) still count."""
    bus, client = rbus
    await bus.publish(ev("crashy"))
    stream = Stream.EVENTS.value
    await bus._ensure_group(stream, "workers")
    # five deliveries to consumers that "die" without acking
    await client.xreadgroup("workers", "dead-0", {stream: ">"}, count=1)
    for i in range(1, 5):
        await client.xautoclaim(stream, "workers", f"dead-{i}", min_idle_time=0, start_id="0-0", count=1)
    [row] = await client.xpending_range(stream, "workers", min="-", max="+", count=1)
    assert row["times_delivered"] == 5

    calls = 0

    async def handler(e: Event) -> None:
        nonlocal calls
        calls += 1

    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))

    async def dead() -> bool:
        return await client.xlen(f"{stream}:dlq") == 1

    await wait_until(dead, timeout=5)
    task.cancel()
    assert calls == 0
    assert (await client.xpending(stream, "workers"))["pending"] == 0


async def test_attempts_come_from_redis_delivery_count(rbus) -> None:
    bus, client = rbus
    seen: list[int] = []

    async def handler(j: Job) -> None:
        seen.append(j.attempts)
        if len(seen) < 3:
            raise RuntimeError("boom")

    await bus.enqueue(Job(id="j1", user_id=1, kind=JobKind.LEARN))
    task = asyncio.create_task(bus.consume_jobs("workers", "w1", handler))
    await wait_until(lambda: len(seen) == 3)
    task.cancel()
    assert seen == [0, 1, 2]


async def test_nogroup_after_redis_restart_recreates_group(rbus) -> None:
    bus, client = rbus
    got: list[Event] = []

    async def handler(e: Event) -> None:
        got.append(e)

    await bus.publish(ev("before"))
    task = asyncio.create_task(bus.consume_events("workers", "w1", handler))
    await wait_until(lambda: len(got) == 1)
    await client.flushall()  # simulate a Redis restart without persistence
    await bus.publish(ev("after"))
    await wait_until(lambda: len(got) == 2, timeout=5)
    task.cancel()
    assert [e.id for e in got] == ["before", "after"]


def test_claim_idle_ms_comes_from_settings(settings, monkeypatch) -> None:
    from mavis.bus import get_bus, set_bus

    assert settings.bus_claim_idle_ms == 900_000
    assert settings.worker_concurrency == 4
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6399/0")
    monkeypatch.setenv("BUS_CLAIM_IDLE_MS", "1234")
    from mavis.config import get_settings

    get_settings.cache_clear()
    set_bus(None)
    try:
        assert get_bus()._claim_idle_ms == 1234
    finally:
        set_bus(None)
