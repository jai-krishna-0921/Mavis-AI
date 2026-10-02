"""Single-active-instance lock for the timer role.

With REDIS_URL set, leadership is a Redis key with a TTL that the leader
refreshes on every acquire(); without Redis (dev) there is only one process.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from redis.asyncio import Redis

from mavis.bus import _make_redis, get_redis


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

    async def acquire(self) -> bool:
        if await self._redis.set(self._key, self._id, nx=True, px=self._ttl):
            return True
        if await self._is_mine():
            await self._redis.pexpire(self._key, self._ttl)
            return True
        return False

    async def release(self) -> None:
        if await self._is_mine():
            await self._redis.delete(self._key)


def make_leader() -> LeaderLock:
    client = get_redis()
    return RedisLeader(client=client) if client is not None else NoopLeader()
