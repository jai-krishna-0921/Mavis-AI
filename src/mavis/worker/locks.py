"""Per-user mutual exclusion: asyncio locks in single-process mode, Redis locks across workers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from weakref import WeakKeyDictionary

from redis.exceptions import LockError

from mavis.bus import get_redis

_local: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()


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
async def user_lock(user_id: int, timeout_s: float = 300) -> AsyncIterator[None]:
    # The in-process lock is always taken first. asyncio.Lock wakes waiters in FIFO order, so events
    # for one user handled by concurrent consumers in this process run in arrival order; the Redis
    # lock then only arbitrates between processes.
    async with lock(f"user:{user_id}", timeout_s):
        yield
