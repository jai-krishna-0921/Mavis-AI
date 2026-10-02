import asyncio
from datetime import timedelta

from mavis.domain import timeutil
from mavis.domain.wakeups import WakeupKind, WakeupStatus
from mavis.timers.service import WakeupService


async def test_wake_me_stores_pending_wakeup(user, clock):
    svc = WakeupService()
    at = timeutil.now() + timedelta(hours=2)
    wid = await svc.wake_me(user.id, at, "check on recruiter", loop_id=None, kind="agent", payload={"x": 1})
    [w] = await svc.pending(user.id)
    assert w.id == wid and w.due_at == at and w.kind is WakeupKind.AGENT
    assert w.payload == {"x": 1} and w.status is WakeupStatus.PENDING


async def test_wake_me_applies_demo_time_scale(user, clock, settings, monkeypatch):
    monkeypatch.setattr(settings, "demo_time_scale", 0.01)
    svc = WakeupService()
    await svc.wake_me(user.id, timeutil.now() + timedelta(hours=1), "scaled")
    await svc.wake_me(user.id, timeutil.now() + timedelta(hours=1), "unscaled", scale=False)
    by_reason = {w.reason: w for w in await svc.pending(user.id)}
    assert by_reason["scaled"].due_at == timeutil.now() + timedelta(seconds=36)
    assert by_reason["unscaled"].due_at == timeutil.now() + timedelta(hours=1)


async def test_dedupe_key_returns_existing_pending(user, clock):
    svc = WakeupService()
    a = await svc.wake_me(user.id, timeutil.now() + timedelta(hours=1), "a", dedupe_key="loop:1:ended")
    b = await svc.wake_me(user.id, timeutil.now() + timedelta(hours=3), "b", dedupe_key="loop:1:ended")
    assert a == b and len(await svc.pending(user.id)) == 1


async def test_claim_due_fires_once(user, clock):
    svc = WakeupService()
    await svc.wake_me(user.id, timeutil.now() + timedelta(minutes=5), "later")
    await svc.wake_me(user.id, timeutil.now() - timedelta(seconds=1), "now")
    first = await svc.claim_due(timeutil.now())
    assert [w.reason for w in first] == ["now"] and first[0].status is WakeupStatus.FIRED
    assert await svc.claim_due(timeutil.now()) == []
    clock.advance(minutes=6)
    assert [w.reason for w in await svc.claim_due(timeutil.now())] == ["later"]


async def test_concurrent_claims_fire_once(user, clock):
    svc = WakeupService()
    await svc.wake_me(user.id, timeutil.now() - timedelta(seconds=1), "pep talk")
    a, b = await asyncio.gather(svc.claim_due(timeutil.now()), svc.claim_due(timeutil.now()))
    assert len(a) + len(b) == 1


async def test_cancel_and_cancel_where(user, clock):
    svc = WakeupService()
    past = timeutil.now() - timedelta(seconds=1)
    w1 = await svc.wake_me(user.id, past, "a")
    await svc.wake_me(user.id, past, "b", loop_id=7, kind=WakeupKind.EVENT_STARTING)
    await svc.wake_me(user.id, past, "c", loop_id=7, kind=WakeupKind.EVENT_ENDED)
    await svc.wake_me(user.id, past, "d", loop_id=8, kind=WakeupKind.EVENT_ENDED)
    assert await svc.cancel(w1) is True
    assert await svc.cancel(w1) is False
    both = [WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED]
    assert await svc.cancel_where(user.id, both, loop_id=7) == 2
    assert [w.reason for w in await svc.claim_due(timeutil.now())] == ["d"]


async def test_reschedule_moves_pending(user, clock):
    svc = WakeupService()
    wid = await svc.wake_me(user.id, timeutil.now() - timedelta(seconds=1), "x")
    assert await svc.reschedule(wid, timeutil.now() + timedelta(hours=1)) is True
    assert await svc.claim_due(timeutil.now()) == []


async def test_system_wakeup_dispatch():
    from mavis.domain.events import Event, EventType, Trust
    from mavis.timers import system

    seen = []

    async def h(user_id, reason):
        seen.append((user_id, reason))

    system.register_system_wakeup("system_poll", h)
    ev = Event(id="wakeup:1", user_id=3, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer",
               payload={"kind": "system_poll", "reason": "gmail"}, trust=Trust.SYSTEM)
    assert await system.dispatch_system_wakeup(ev) is True
    agent = ev.model_copy(update={"payload": {"kind": "agent", "reason": "x"}})
    assert await system.dispatch_system_wakeup(agent) is False
    assert seen == [(3, "gmail")]
    system.SYSTEM_WAKEUP_HANDLERS.clear()


async def test_fire_due_publishes_before_marking_and_retries_on_failure(user, clock):
    svc = WakeupService()
    past = timeutil.now() - timedelta(seconds=1)
    await svc.wake_me(user.id, past, "one")
    await svc.wake_me(user.id, past, "two")
    seen: list[str] = []

    async def flaky(w):
        if w.reason == "two":
            raise RuntimeError("redis blip")
        seen.append(w.reason)

    try:
        await svc.fire_due(timeutil.now(), flaky)
    except RuntimeError:
        pass
    assert seen == ["one"]
    assert [w.reason for w in await svc.pending(user.id)] == ["two"]

    async def ok(w):
        seen.append(w.reason)

    assert [w.reason for w in await svc.fire_due(timeutil.now(), ok)] == ["two"]
    assert seen == ["one", "two"] and await svc.pending(user.id) == []
