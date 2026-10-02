"""The `timer` role: turns due wakeups into events. No business logic here."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import structlog

from mavis.bus.base import EventBus
from mavis.bus.leader import LeaderLock, make_leader
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Event, Trust
from mavis.domain.wakeups import EVENT_TYPE_FOR_KIND, Wakeup
from mavis.loops.service import LoopService
from mavis.timers.service import WakeupService

log = structlog.get_logger()
EXPIRY_EVERY = timedelta(hours=1)


def wakeup_event(w: Wakeup) -> Event:
    return Event(
        id=f"wakeup:{w.id}",
        user_id=w.user_id,
        type=EVENT_TYPE_FOR_KIND[w.kind],
        occurred_at=w.due_at,
        source="timer",
        payload={"wakeup_id": w.id, "kind": w.kind.value, "reason": w.reason, "loop_id": w.loop_id,
                 **w.payload},
        trust=Trust.SYSTEM,
    )


class TimerRunner:
    def __init__(self, bus: EventBus, wakeups: WakeupService, leader: LeaderLock, interval_s: float,
                 loops: LoopService | None = None) -> None:
        self._bus, self._wakeups, self._leader, self._interval = bus, wakeups, leader, interval_s
        self._loops = loops
        self._last_expiry: datetime | None = None

    async def _publish(self, w: Wakeup) -> None:
        await self._bus.publish(wakeup_event(w))

    async def tick(self) -> int:
        if not await self._leader.acquire():
            return 0
        now = timeutil.now()
        # publish first, mark fired after: a failed publish leaves the wakeup pending for the next tick
        fired = await self._wakeups.fire_due(now, self._publish)
        if self._loops is not None and (self._last_expiry is None or now - self._last_expiry >= EXPIRY_EVERY):
            expired = await self._loops.expire_stale()
            self._last_expiry = now
            if expired:
                log.info("timer.loops_expired", count=expired)
        if fired:
            log.info("timer.fired", count=len(fired))
        return len(fired)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - the timer must survive transient DB/Redis errors
                    log.exception("timer.tick_failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
        finally:
            await self._leader.release()


async def run_timer(stop: asyncio.Event | None = None) -> None:
    from mavis.bus import get_bus

    bus = get_bus()
    runner = TimerRunner(bus, WakeupService(), make_leader(), get_settings().timer_interval_s,
                         loops=LoopService(bus))
    await runner.run_forever(stop)
