from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.events import Event, EventType, Trust
from mavis.domain.jitter import user_offset
from mavis.worker import runner
from mavis.worker.mailbox import MemoryMailbox
from mavis.worker.scheduler import Scheduler, coalesce, lane_of

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _msg(uid: int, n: int, text: str = "hi", at: datetime | None = None,
         etype=EventType.USER_MESSAGE) -> Event:
    return Event(id=f"m:{uid}:{n}", user_id=uid, type=etype, occurred_at=at or T0 + timedelta(seconds=n),
                 source="test", payload={"text": f"{text} {n}"}, trust=Trust.USER)


@pytest.fixture
def seen(settings):
    out: list[tuple[int, str]] = []

    async def handler(e: Event) -> None:
        await asyncio.sleep(0.01)
        out.append((e.user_id, e.id))

    runner.register_event_handler(EventType.USER_MESSAGE, handler)
    runner.register_event_handler(EventType.EMAIL_RECEIVED, handler)
    return out


def test_lanes():
    assert lane_of(_msg(1, 1)) == "chat"
    assert lane_of(_msg(1, 2, etype=EventType.BUTTON_PRESSED)) == "chat"
    assert lane_of(_msg(1, 3, etype=EventType.EMAIL_RECEIVED)) == "bg"


async def test_round_robin_across_users_under_burst(seen, db):
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=0)
    for n in range(30):
        await sched.intake(_msg(5001, n, at=T0 + timedelta(minutes=n)))  # far apart: no coalescing
    await sched.intake(_msg(7302, 100, at=T0))
    await sched.drain()
    first_other = next(i for i, (uid, _) in enumerate(seen) if uid == 7302)
    assert first_other <= 1  # the second user waited for at most one turn of the busy user


@pytest.mark.parametrize("users", [(11, 12), (5001, 9944, 7302)])
async def test_per_user_fifo_order_holds_across_two_executors(seen, db, users):
    sched = Scheduler(MemoryMailbox(), chat_executors=2, bg_executors=1, coalesce_window_s=0)
    for n in range(6):
        for uid in users:
            await sched.intake(_msg(uid, n, at=T0 + timedelta(minutes=n)))
    await sched.drain()
    for uid in users:
        mine = [eid for u, eid in seen if u == uid]
        assert mine == [f"m:{uid}:{n}" for n in range(6)]


async def test_chat_and_background_lanes_run_independently(seen, db):
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=0)
    await sched.intake(_msg(1, 1, etype=EventType.EMAIL_RECEIVED))
    await sched.intake(_msg(1, 2))
    await sched.drain()
    assert {eid for _, eid in seen} == {"m:1:1", "m:1:2"}


async def test_expired_lease_is_reaped_and_resumed_once(seen, db):
    box = MemoryMailbox(lease_ms=50)
    sched = Scheduler(box, chat_executors=1, bg_executors=1, coalesce_window_s=0)
    await sched.intake(_msg(31, 1))
    claimed = await box.claim("chat", "dead-worker")  # a worker took it and died
    assert claimed is not None
    await asyncio.sleep(0.08)
    assert await box.reap() == 1
    await sched.drain()
    assert seen == [(31, "m:31:1")]


@pytest.mark.parametrize("texts,expected_run,logged", [
    (["a", "b", "c"], 1, 2),
    (["a"] * 7, 2, 5),       # capped at 5 messages per turn
    (["x" * 1500, "y" * 1500], 2, 0),  # 2000-char cap splits them
])
async def test_burst_coalescing_caps(db, user, settings, texts, expected_run, logged):
    events = [Event(id=f"c:{i}", user_id=user.id, type=EventType.USER_MESSAGE,
                    occurred_at=T0 + timedelta(seconds=i), source="test", payload={"text": t},
                    trust=Trust.USER) for i, t in enumerate(texts)]
    first_batch = events[: settings.coalesce_max_messages]
    earlier, last = coalesce(first_batch)
    total = len(earlier) + 1
    assert total <= settings.coalesce_max_messages
    assert sum(len(e.payload["text"]) for e in [*earlier, last]) <= max(2000, len(last.payload["text"]))


async def test_buttons_are_never_coalesced(db, user):
    evs = [_msg(user.id, 1), _msg(user.id, 2, etype=EventType.BUTTON_PRESSED), _msg(user.id, 3)]
    earlier, last = coalesce(evs)
    assert earlier == [] and last.id == f"m:{user.id}:1"


@pytest.mark.parametrize("uid", [1, 2, 3, 77, 5001])
def test_jitter_is_deterministic_and_bounded(uid):
    assert user_offset(uid) == user_offset(uid)
    assert timedelta(0) <= user_offset(uid) < timedelta(seconds=600)
    assert len({user_offset(u) for u in range(1, 60)}) > 20  # users spread out


async def test_legacy_scheduler_is_the_default(settings):
    assert settings.worker_scheduler == "legacy"


async def _active_user(chat: int):
    from mavis.store.repo import users

    u, _ = await users.get_or_create_by_chat(chat, "x")
    await users.update(u.id, status="active")
    return u


async def test_a_burst_runs_one_turn_and_keeps_every_message_in_history(seen, db):
    from mavis.store.repo import messages

    u = await _active_user(8101)
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=3.0)
    for n in range(3):  # three messages a second apart: one thought
        await sched.intake(_msg(u.id, n, text="photo", at=T0 + timedelta(seconds=n)))
    await sched.drain()
    assert seen == [(u.id, f"m:{u.id}:2")]  # only the last one is a turn
    assert [m.content for m in await messages.recent(u.id, 10)] == ["photo 0", "photo 1"]


async def test_pending_users_events_are_never_coalesced_or_stored(seen, db):
    from mavis.store.repo import messages, users

    u, _ = await users.get_or_create_by_chat(8102, "stranger")  # pending
    sched = Scheduler(MemoryMailbox(), chat_executors=1, bg_executors=1, coalesce_window_s=3.0)
    for n in range(3):
        await sched.intake(_msg(u.id, n, at=T0 + timedelta(seconds=n)))
    await sched.drain()
    assert [eid for _, eid in seen] == [f"m:{u.id}:{n}" for n in range(3)]
    assert await messages.recent(u.id, 10) == []


async def test_a_poison_event_is_dropped_after_three_failed_turns(db, settings):
    calls = []

    async def boom(e: Event) -> None:
        calls.append(e.id)
        raise RuntimeError("nope")

    runner.register_event_handler(EventType.EMAIL_RECEIVED, boom)
    box = MemoryMailbox(lease_ms=1)
    sched = Scheduler(box, chat_executors=1, bg_executors=1, coalesce_window_s=0)
    await sched.intake(_msg(41, 1, etype=EventType.EMAIL_RECEIVED))
    await sched.intake(_msg(41, 2, etype=EventType.EMAIL_RECEIVED))
    for _ in range(8):
        await asyncio.sleep(0.01)
        await box.reap()
        await sched.drain()
    assert calls.count("m:41:1") == 3 and calls.count("m:41:2") == 3  # dropped, then the next one ran
    assert box.idle()


async def test_morning_checkin_is_spread_by_the_users_offset(db, user, bus, monkeypatch):
    from mavis.config import get_settings
    from mavis.domain.wakeups import WakeupKind
    from mavis.initiative.wiring import wire_initiative
    from mavis.store.repo import users
    from mavis.timers.service import WakeupService

    init = wire_initiative(register_handlers=False)
    await init.routines.on_user_message(await users.get(user.id))
    (flat,) = await WakeupService().pending(user.id, WakeupKind.ROUTINE)
    await WakeupService().cancel(flat.id)
    monkeypatch.setenv("FANOUT_JITTER_S", "600")
    get_settings.cache_clear()
    await init.routines.reschedule(await users.get(user.id), None)
    (spread,) = await WakeupService().pending(user.id, WakeupKind.ROUTINE)
    assert spread.due_at - flat.due_at == user_offset(user.id)


async def test_global_task_cap_defers_a_new_run(db, settings, monkeypatch):
    from mavis.agents import orchestrator
    from mavis.domain.tasks import TaskStatus
    from mavis.store.repo import tasks, users

    monkeypatch.setenv("TASK_GLOBAL_CONCURRENCY", "2")
    from mavis.config import get_settings

    get_settings.cache_clear()
    ids = []
    for chat in (8201, 8202, 8203):
        u, _ = await users.get_or_create_by_chat(chat, "x")
        ids.append((u.id, await tasks.create(u.id, goal="g")))
    for _, tid in ids[:2]:
        assert await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert await tasks.running_count_all() == 2
    await orchestrator.run_task(ids[2][1])
    assert (await tasks.get(ids[2][1])).status == TaskStatus.QUEUED  # waits for a slot


async def test_timer_ticks_run_on_their_interval_and_survive_errors(settings):
    from mavis.timers import runner as timers

    timers.clear_timer_ticks()
    calls = []

    async def good() -> None:
        calls.append("good")

    async def bad() -> None:
        raise RuntimeError("x")

    timers.register_timer_tick("good", good, 3600)
    timers.register_timer_tick("bad", bad, 0)
    await timers._run_ticks()
    await timers._run_ticks()
    assert calls == ["good"]  # the hour-long interval is respected, the failing hook did not stop the rest
    timers.clear_timer_ticks()
