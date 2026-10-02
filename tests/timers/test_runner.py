import asyncio
from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.events import EventType
from mavis.domain.loops import LoopKind, LoopStatus, LoopUpsert
from mavis.domain.wakeups import WakeupKind
from mavis.loops.service import LoopService
from mavis.timers.runner import TimerRunner
from mavis.timers.service import WakeupService


class DenyLeader:
    async def acquire(self) -> bool:
        return False

    async def release(self) -> None:
        return None


class AllowLeader(DenyLeader):
    async def acquire(self) -> bool:
        return True


async def test_tick_publishes_mapped_events(user, clock, recording_bus):
    wakeups = WakeupService()
    past = timeutil.now() - timedelta(seconds=1)
    await wakeups.wake_me(user.id, past, "pep", loop_id=3, kind=WakeupKind.EVENT_STARTING)
    await wakeups.wake_me(user.id, past, "nudge", kind=WakeupKind.USER_QUIET, payload={"streak": 0})
    await wakeups.wake_me(user.id, past, "morning", kind=WakeupKind.ROUTINE,
                          payload={"routine": "morning_checkin"})
    runner = TimerRunner(recording_bus, wakeups, AllowLeader(), interval_s=0.01)
    assert await runner.tick() == 3
    events = {e.payload["reason"]: e for e in recording_bus.take()}
    assert events["pep"].type is EventType.EVENT_STARTING and events["pep"].payload["loop_id"] == 3
    assert events["nudge"].type is EventType.USER_QUIET and events["nudge"].payload["streak"] == 0
    assert events["morning"].type is EventType.WAKEUP
    assert events["morning"].payload["routine"] == "morning_checkin"
    assert all(e.id == f"wakeup:{e.payload['wakeup_id']}" and e.source == "timer" for e in events.values())


async def test_tick_does_nothing_without_leadership(user, clock, recording_bus):
    wakeups = WakeupService()
    await wakeups.wake_me(user.id, timeutil.now() - timedelta(seconds=1), "x")
    assert await TimerRunner(recording_bus, wakeups, DenyLeader(), interval_s=0.01).tick() == 0
    assert recording_bus.events == []
    assert len(await wakeups.pending(user.id)) == 1


async def test_tick_expires_stale_loops_hourly(user, clock, recording_bus):
    loops = LoopService(recording_bus)
    stale = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="old",
                                                   due_at=timeutil.now() - timedelta(days=3)))
    runner = TimerRunner(recording_bus, WakeupService(), AllowLeader(), interval_s=0.01, loops=loops)
    await runner.tick()
    assert (await loops.get(stale.id)).status is LoopStatus.EXPIRED


async def test_failed_publish_leaves_wakeup_pending_and_retries(user, clock, recording_bus, monkeypatch):
    wakeups = WakeupService()
    await wakeups.wake_me(user.id, timeutil.now() - timedelta(seconds=1), "retry me")
    runner = TimerRunner(recording_bus, wakeups, AllowLeader(), interval_s=0.01)
    real = recording_bus.publish

    async def boom(event):
        raise ConnectionError("redis blip")

    monkeypatch.setattr(recording_bus, "publish", boom)
    with pytest.raises(ConnectionError):
        await runner.tick()
    assert len(await wakeups.pending(user.id)) == 1
    monkeypatch.setattr(recording_bus, "publish", real)
    assert await runner.tick() == 1
    assert await wakeups.pending(user.id) == []
    assert [e.payload["reason"] for e in recording_bus.take()] == ["retry me"]


async def test_run_forever_releases_leader_on_stop(user, clock, recording_bus):
    released: list[bool] = []

    class Tracking(AllowLeader):
        async def release(self) -> None:
            released.append(True)

    stop = asyncio.Event()
    runner = TimerRunner(recording_bus, WakeupService(), Tracking(), interval_s=0.01)
    task = asyncio.create_task(runner.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert released == [True]


async def test_payload_cannot_overwrite_reserved_event_fields(user, clock):
    from mavis.domain.wakeups import Wakeup
    from mavis.timers.runner import wakeup_event

    w = Wakeup(id=9, user_id=user.id, due_at=timeutil.now(), kind=WakeupKind.AGENT, reason="r", loop_id=2,
               payload={"kind": "system_poll", "reason": "evil", "loop_id": 99, "wakeup_id": 1, "x": 1})
    p = wakeup_event(w).payload
    assert p["kind"] == "agent" and p["reason"] == "r" and p["loop_id"] == 2 and p["wakeup_id"] == 9
    assert p["x"] == 1
