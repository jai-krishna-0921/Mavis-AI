import fakeredis

from mavis.bus.leader import NoopLeader, RedisLeader


async def test_noop_leader_always_leads():
    assert await NoopLeader().acquire() is True


async def test_only_one_redis_leader_at_a_time():
    server = fakeredis.FakeServer()
    a = RedisLeader(client=fakeredis.FakeAsyncRedis(server=server))
    b = RedisLeader(client=fakeredis.FakeAsyncRedis(server=server))
    assert await a.acquire() is True
    assert await b.acquire() is False
    assert await a.acquire() is True  # refresh keeps leadership
    await a.release()
    assert await b.acquire() is True
