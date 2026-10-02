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


class _ScriptedRedis:
    """Stands in for a Redis with Lua: records eval calls and answers like the scripts would."""

    def __init__(self, value: str | None, *, scripting: bool = True) -> None:
        self.value, self.scripting, self.evals = value, scripting, []

    async def set(self, key, val, nx=False, px=None):
        return False  # someone holds it

    async def eval(self, script, numkeys, key, *args):
        if not self.scripting:
            from redis.exceptions import ResponseError

            raise ResponseError("unknown command 'eval'")
        self.evals.append((script, args))
        return 1 if args[0] == self.value else 0

    async def get(self, key):
        return self.value


async def test_leader_uses_atomic_scripts_when_available():
    mine = RedisLeader(client=_ScriptedRedis(None))
    mine._redis.value = mine._id
    assert await mine.acquire() is True
    await mine.release()
    scripts = [s for s, _ in mine._redis.evals]
    assert "pexpire" in scripts[0] and "'del'" in scripts[1]
    other = RedisLeader(client=_ScriptedRedis("someone-else"))
    assert await other.acquire() is False


async def test_leader_falls_back_when_scripting_unsupported():
    r = _ScriptedRedis(None, scripting=False)
    leader = RedisLeader(client=r)
    r.value = leader._id

    async def pexpire(key, ttl):
        return 1

    r.pexpire = pexpire
    assert await leader.acquire() is True
