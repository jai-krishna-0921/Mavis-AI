"""Per-user mutual exclusion: asyncio locks in single-process mode, Redis locks across workers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from weakref import WeakKeyDictionary

from redis.exceptions import LockError

from zento.bus import get_redis

_local: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()


@contextlib.asynccontextmanager
async def user_lock(user_id: int, timeout_s: float = 300) -> AsyncIterator[None]:
    client = get_redis()
    if client is None:
        locks = _local.setdefault(asyncio.get_running_loop(), {})
        async with locks.setdefault(user_id, asyncio.Lock()):
            yield
        return
    lock = client.lock(f"zento:lock:user:{user_id}", timeout=timeout_s, blocking_timeout=timeout_s)
    if not await lock.acquire():
        raise TimeoutError(f"could not acquire lock for user {user_id}")
    try:
        yield
    finally:
        with contextlib.suppress(LockError):
            await lock.release()
