"""Per-user mailboxes (spec 7.1): a FIFO list per (user, lane), a ready ZSET per lane scored by arrival,
and a lease per (user, lane). One lease per user and lane gives per-user order across processes; re-adding
a busy user at the back after each turn gives round robin across users."""

from __future__ import annotations

import time
from typing import Literal, Protocol

Lane = Literal["chat", "bg"]


def _box(uid: int, lane: str) -> str:
    return f"mavis:mbox:u{uid}:{lane}"


def _lease(uid: int, lane: str) -> str:
    return f"mavis:lease:u{uid}:{lane}"


class MailboxBackend(Protocol):
    async def push(self, uid: int, lane: Lane, data: str) -> None: ...
    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None: ...
    async def commit(self, uid: int, lane: Lane, n: int) -> None: ...
    async def release(self, uid: int, lane: Lane, owner: str) -> None: ...
    async def renew(self, uid: int, lane: Lane, owner: str) -> bool: ...
    async def reap(self) -> int: ...
    async def depth(self, lane: Lane) -> int: ...


PUSH = r"""
redis.call('RPUSH', KEYS[1], ARGV[1])
if redis.call('LLEN', KEYS[1]) > tonumber(ARGV[4]) then redis.call('LPOP', KEYS[1]) end
if redis.call('EXISTS', KEYS[2]) == 0 then redis.call('ZADD', KEYS[3], 'NX', ARGV[2], ARGV[3]) end
return 1
"""
# KEYS[1] ready zset; ARGV owner, lease_ms, take, lane. Pops ready users until one is leasable.
CLAIM = r"""
for i = 1, 50 do
  local top = redis.call('ZPOPMIN', KEYS[1])
  if #top == 0 then return nil end
  local u = top[1]
  local box = 'mavis:mbox:' .. u .. ':' .. ARGV[4]
  if redis.call('LLEN', box) > 0 then
    if redis.call('SET', 'mavis:lease:' .. u .. ':' .. ARGV[4], ARGV[1], 'NX', 'PX', ARGV[2]) then
      return {u, redis.call('LRANGE', box, 0, tonumber(ARGV[3]) - 1)}
    end
  end
end
return nil
"""
RELEASE = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
if redis.call('LLEN', KEYS[2]) > 0 then redis.call('ZADD', KEYS[3], ARGV[2], ARGV[3]) end
return 1
"""


class RedisMailbox:
    def __init__(self, client, *, lease_ms: int = 120_000, take: int = 5, cap: int = 200) -> None:
        self._r, self._lease_ms, self._take, self._cap = client, lease_ms, take, cap
        self._push = client.register_script(PUSH)
        self._claim = client.register_script(CLAIM)
        self._release = client.register_script(RELEASE)

    async def push(self, uid: int, lane: Lane, data: str) -> None:
        await self._push(keys=[_box(uid, lane), _lease(uid, lane), f"mavis:ready:{lane}"],
                         args=[data, int(time.time() * 1000), f"u{uid}", self._cap])

    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None:
        res = await self._claim(keys=[f"mavis:ready:{lane}"], args=[owner, self._lease_ms, self._take, lane])
        if not res:
            return None
        return int(str(res[0])[1:]), list(res[1])

    async def commit(self, uid: int, lane: Lane, n: int) -> None:
        await self._r.ltrim(_box(uid, lane), n, -1)

    async def release(self, uid: int, lane: Lane, owner: str) -> None:
        await self._release(keys=[_lease(uid, lane), _box(uid, lane), f"mavis:ready:{lane}"],
                            args=[owner, int(time.time() * 1000), f"u{uid}"])

    async def renew(self, uid: int, lane: Lane, owner: str) -> bool:
        if await self._r.get(_lease(uid, lane)) == owner:
            return bool(await self._r.pexpire(_lease(uid, lane), self._lease_ms))
        return False

    async def reap(self) -> int:
        n = 0
        async for key in self._r.scan_iter(match="mavis:mbox:u*:*", count=200):
            _, _, u, lane = key.split(":")
            if not await self._r.exists(f"mavis:lease:{u}:{lane}") and await self._r.llen(key) > 0:
                n += int(await self._r.zadd(f"mavis:ready:{lane}", {u: int(time.time() * 1000)}, nx=True))
        return n

    async def depth(self, lane: Lane) -> int:
        return int(await self._r.zcard(f"mavis:ready:{lane}"))


class MemoryMailbox:
    """Same semantics in one process (dev, tests, `mavis chat`)."""

    def __init__(self, *, lease_ms: int = 120_000, take: int = 5, cap: int = 200) -> None:
        self._boxes: dict[tuple[int, str], list[str]] = {}
        self._ready: dict[str, dict[int, float]] = {"chat": {}, "bg": {}}
        self._leases: dict[tuple[int, str], tuple[str, float]] = {}
        self._lease_s, self._take, self._cap = lease_ms / 1000, take, cap

    def _leased(self, uid: int, lane: str) -> bool:
        lease = self._leases.get((uid, lane))
        return lease is not None and lease[1] > time.monotonic()

    async def push(self, uid: int, lane: Lane, data: str) -> None:
        box = self._boxes.setdefault((uid, lane), [])
        box.append(data)
        del box[:-self._cap]
        if not self._leased(uid, lane):
            self._ready[lane].setdefault(uid, time.monotonic())

    async def claim(self, lane: Lane, owner: str) -> tuple[int, list[str]] | None:
        for uid, _ in sorted(self._ready[lane].items(), key=lambda kv: kv[1]):
            del self._ready[lane][uid]
            box = self._boxes.get((uid, lane)) or []
            if box and not self._leased(uid, lane):
                self._leases[(uid, lane)] = (owner, time.monotonic() + self._lease_s)
                return uid, list(box[: self._take])
        return None

    async def commit(self, uid: int, lane: Lane, n: int) -> None:
        del self._boxes.get((uid, lane), [])[:n]

    async def release(self, uid: int, lane: Lane, owner: str) -> None:
        if (lease := self._leases.get((uid, lane))) and lease[0] == owner:
            del self._leases[(uid, lane)]
        if self._boxes.get((uid, lane)):
            self._ready[lane][uid] = time.monotonic()

    async def renew(self, uid: int, lane: Lane, owner: str) -> bool:
        if (lease := self._leases.get((uid, lane))) and lease[0] == owner:
            self._leases[(uid, lane)] = (owner, time.monotonic() + self._lease_s)
            return True
        return False

    async def reap(self) -> int:
        n = 0
        for (uid, lane), box in self._boxes.items():
            if box and not self._leased(uid, lane) and uid not in self._ready[lane]:
                self._ready[lane][uid] = time.monotonic()
                n += 1
        return n

    async def depth(self, lane: Lane) -> int:
        return len(self._ready[lane])

    def idle(self) -> bool:
        return not any(self._boxes.values()) and not any(self._leased(u, ln) for u, ln in self._leases)
