"""Per-user machine quotas (owner decision 3: $5/month, 60 minutes/day), metering and global slots.

Metering is deliberately an upper bound: wall seconds x configured vCPU and GB x list price x
MACHINE_ASSUMED_ACTIVE_FRACTION (1.0). Plan 11 can swap in a plan-based QuotaPolicy without touching this."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mavis.bus import get_redis
from mavis.config import get_settings
from mavis.store.db import utcnow
from mavis.store.repo import machine as repo
from mavis.store.repo import users

DAILY_TEXT = "You've used today's machine time. It resets at midnight your time."
MONTHLY_TEXT = "You've reached this month's machine budget. It resets on the 1st."
BUSY_TEXT = "My machine is busy right now. Try again in a few minutes."
CONCURRENT_TEXT = "I'm already running one machine task for you. I'll take this on when it's done."
QUOTA_KEYS = {"daily_minutes": "machine_user_daily_minutes", "monthly_usd": "machine_user_monthly_usd",
              "concurrent": "machine_max_concurrent_per_user"}


def local_day(tz: str) -> date:
    """The user's calendar day now, on the injectable clock; an unknown zone name falls back to UTC."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return utcnow().astimezone(zone).date()


class QuotaPolicy(Protocol):
    async def limit(self, user_id: int, key: str, default: float) -> float: ...


class SettingsQuotaPolicy:
    async def limit(self, user_id: int, key: str, default: float) -> float:
        override = await repo.quota_override(user_id, key)
        return float(default if override is None else override)


class Meter:
    def __init__(self, quota: QuotaPolicy | None = None) -> None:
        self.quota = quota or SettingsQuotaPolicy()

    def cost(self, kind: str, wall_s: float) -> float:
        s = get_settings()
        if kind == "browser":
            vcpu, gb = s.machine_browser_vcpu, s.machine_browser_gb
        else:
            vcpu, gb = s.machine_ci_vcpu, s.machine_ci_gb
        hours = max(0.0, wall_s) / 3600 * s.machine_assumed_active_fraction
        return hours * (vcpu * s.machine_price_vcpu_hour + gb * s.machine_price_gb_hour)

    async def record(self, user_id: int, kind: str, task_id: int | None, session_id: str, wall_s: float,
                     provider: str = "agentcore") -> float:
        tz = (await users.get(user_id)).timezone
        cost = self.cost(kind, wall_s)
        await repo.record_usage(user_id, local_day(tz), provider, kind, task_id=task_id,
                                session_id=session_id, wall_s=wall_s, est_cost_usd=cost)
        return cost

    async def refusal(self, user_id: int) -> str | None:
        s = get_settings()
        tz = (await users.get(user_id)).timezone
        today = local_day(tz)
        minutes = await self.quota.limit(user_id, "daily_minutes", s.machine_user_daily_minutes)
        if await repo.minutes_on(user_id, today) >= minutes:
            return DAILY_TEXT
        budget = await self.quota.limit(user_id, "monthly_usd", s.machine_user_monthly_usd)
        if await repo.spend_between(user_id, today.replace(day=1), today) >= budget:
            return MONTHLY_TEXT
        return None


class GlobalSlots:
    KEY = "mavis:machine:slots"

    def __init__(self, size: int | None = None) -> None:
        self.size = int(size if size is not None else get_settings().machine_max_concurrent)
        self._local: dict[int, float] = {}

    def _lease_ms(self) -> int:
        s = get_settings()
        return int((s.machine_task_timeout_s + s.machine_session_grace_s) * 1000)

    async def _try(self, task_id: int) -> bool:
        now_ms = int(time.time() * 1000)
        client = get_redis()
        if client is None:
            self._local = {k: v for k, v in self._local.items() if v > now_ms}
            if task_id in self._local or len(self._local) < self.size:
                self._local[task_id] = now_ms + self._lease_ms()
                return True
            return False
        await client.zremrangebyscore(self.KEY, 0, now_ms)
        if await client.zscore(self.KEY, str(task_id)) is not None:
            return True
        if await client.zcard(self.KEY) >= self.size:
            return False
        await client.zadd(self.KEY, {str(task_id): now_ms + self._lease_ms()})
        if await client.zcard(self.KEY) > self.size:  # lost a race: back out
            await client.zrem(self.KEY, str(task_id))
            return False
        return True

    async def acquire(self, task_id: int, wait_s: float | None = None) -> bool:
        deadline = time.monotonic() + (get_settings().machine_global_wait_s if wait_s is None else wait_s)
        while True:
            if await self._try(task_id):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def release(self, task_id: int) -> None:
        self._local.pop(task_id, None)
        if (client := get_redis()) is not None:
            await client.zrem(self.KEY, str(task_id))

    async def held(self) -> list[int]:
        client = get_redis()
        if client is None:
            now_ms = int(time.time() * 1000)
            return [k for k, v in self._local.items() if v > now_ms]
        return [int(m) for m in await client.zrangebyscore(self.KEY, int(time.time() * 1000), "+inf")]
