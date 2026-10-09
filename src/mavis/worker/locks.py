"""Per-user mutual exclusion: asyncio locks in single-process mode, Redis locks across workers."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from weakref import WeakKeyDictionary

from redis.exceptions import LockError

from mavis.bus import get_redis

_local: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()

_claims: dict[str, float] = {}


async def claim(key: str, ttl_s: float) -> bool:
    """Atomic set-if-absent with expiry (Redis SET NX EX, in-process dict in dev). True if this caller won."""
    client = get_redis()
    if client is not None:
        return bool(await client.set(f"mavis:claim:{key}", "1", nx=True, ex=max(1, int(ttl_s))))
    now = time.monotonic()
    for k in [k for k, exp in _claims.items() if exp <= now]:
        del _claims[k]
    if key in _claims:
        return False
    _claims[key] = now + ttl_s
    return True


async def release(key: str) -> None:
    """Drop a claim early, so the next `claim(key, ...)` can win (a connection removed and made again)."""
    client = get_redis()
    if client is not None:
        await client.delete(f"mavis:claim:{key}")
        return
    _claims.pop(key, None)


@contextlib.asynccontextmanager
async def lock(key: str, timeout_s: float = 300) -> AsyncIterator[None]:
    """Named mutual exclusion (in-process FIFO asyncio lock, then a Redis lock across processes)."""
    locks = _local.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(key, asyncio.Lock()):
        client = get_redis()
        if client is None:
            yield
            return
        redis_lock = client.lock(f"mavis:lock:{key}", timeout=timeout_s, blocking_timeout=timeout_s)
        if not await redis_lock.acquire():
            raise TimeoutError(f"could not acquire lock {key}")
        try:
            yield
        finally:
            with contextlib.suppress(LockError):
                await redis_lock.release()


@contextlib.asynccontextmanager
async def user_lock(user_id: int, timeout_s: float = 600) -> AsyncIterator[None]:
    # The in-process lock is always taken first. asyncio.Lock wakes waiters in FIFO order, so events
    # for one user handled by concurrent consumers in this process run in arrival order; the Redis
    # lock then only arbitrates between processes.
    async with lock(f"user:{user_id}", timeout_s):
        yield
