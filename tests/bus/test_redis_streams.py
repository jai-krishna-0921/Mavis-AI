import asyncio
from datetime import UTC, datetime

import pytest
from fakeredis import FakeAsyncRedis

from tests.fakes import wait_until
from zento.bus.base import Stream
from zento.bus.redis_streams import RedisStreamsBus
from zento.domain.events import Event, EventType, Job, JobKind


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
    assert 0 < await client.ttl("zento:seen:tg:update:7") <= 7 * 24 * 3600


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
    from zento.bus import get_bus, get_redis, set_bus
    from zento.bus.inprocess import InProcessBus

    set_bus(None)
    try:
        assert isinstance(get_bus(), InProcessBus)
        assert get_redis() is None
    finally:
        set_bus(None)
