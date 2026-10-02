"""Single-active-instance lock for the timer role.

With REDIS_URL set, leadership is a Redis key with a TTL that the leader
refreshes on every acquire(); without Redis (dev) there is only one process.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from mavis.bus import _make_redis, get_redis

# compare-and-pexpire / compare-and-delete: never touch a lock another instance took after our TTL lapsed
_REFRESH = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('pexpire', KEYS[1], ARGV[2]) end return 0"
)
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


class LeaderLock(Protocol):
    async def acquire(self) -> bool: ...
    async def release(self) -> None: ...


class NoopLeader:
    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class RedisLeader:
    def __init__(self, url: str | None = None, *, client: Redis | None = None,
                 key: str = "mavis:timer:leader", ttl_ms: int = 15_000) -> None:
        if client is None:
            client = _make_redis(url) if url else get_redis()
        if client is None:
            raise ValueError("RedisLeader needs a client, a url or REDIS_URL")
        self._redis = client
        self._key, self._ttl, self._id = key, ttl_ms, uuid4().hex

    async def _is_mine(self) -> bool:
        current = await self._redis.get(self._key)
        if isinstance(current, bytes):
            current = current.decode()
        return current == self._id

    async def _eval(self, script: str, *args: object) -> int | None:
        """Run an atomic Lua script; None if scripting is unavailable (fakeredis), so callers fall back."""
        try:
            return int(await self._redis.eval(script, 1, self._key, *args))
        except ResponseError:
            return None

    async def acquire(self) -> bool:
        if await self._redis.set(self._key, self._id, nx=True, px=self._ttl):
            return True
        refreshed = await self._eval(_REFRESH, self._id, self._ttl)
        if refreshed is not None:
            return refreshed == 1
        if await self._is_mine():  # non-atomic fallback
            await self._redis.pexpire(self._key, self._ttl)
            return True
        return False

    async def release(self) -> None:
        if await self._eval(_RELEASE, self._id) is None and await self._is_mine():
            await self._redis.delete(self._key)


def make_leader() -> LeaderLock:
    client = get_redis()
    return RedisLeader(client=client) if client is not None else NoopLeader()
