from datetime import UTC, datetime, time, timedelta

import pytest

from mavis.channels.outbox_sender import deliver_pending
from mavis.domain.decisions import ComposedMessage
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import routines as routines_mod
from mavis.initiative.composer import Composer
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.routines import MORNING_ROUTINE, MORNING_TITLE, BriefItem, Routines
from mavis.loops.service import LoopService
from mavis.policy.pings import PingPolicy
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.timers.service import WakeupService


@pytest.fixture(autouse=True)
def _no_sources():
    routines_mod.clear_brief_sources()
    yield
    routines_mod.clear_brief_sources()


def build(bus, memory):
    wakeups, loops = WakeupService(), LoopService(bus)
    executor = InitiativeExecutor(bus, loops, wakeups, PingPolicy(), Composer(memory), QuietTracker(wakeups))
    return Routines(loops, wakeups, executor), loops, wakeups


async def test_first_message_seeds_morning_routine_once(user, clock, recording_bus, fake_memory):
    routines, loops, wakeups = build(recording_bus, fake_memory)
    await routines.on_user_message(user)
    await routines.on_user_message(user)
    assert [lp.title for lp in await loops.active(user.id) if lp.kind is LoopKind.ROUTINE] == [MORNING_TITLE]
    [w] = await wakeups.pending(user.id, WakeupKind.ROUTINE)
    assert w.payload["routine"] == MORNING_ROUTINE
    assert w.due_at == datetime(2026, 9, 28, 3, 0, tzinfo=UTC)  # Mon 08:30 IST (clock: Sun 13:30 IST)


async def test_learned_time_from_first_messages(user, clock, recording_bus, fake_memory):
    clock.set(datetime(2026, 10, 1, 6, 30, tzinfo=UTC))  # Thu 12:00 IST
    ist_to_utc = lambda d, h, m: datetime(2026, 9, d, h, m, tzinfo=UTC) - timedelta(hours=5, minutes=30)  # noqa: E731
    async with Session() as s:
        stamps = (ist_to_utc(28, 10, 5), ist_to_utc(28, 15, 0), ist_to_utc(29, 10, 20), ist_to_utc(30, 9, 55))
        for created in stamps:
            s.add(Message(user_id=user.id, role="user", content="hi", proactive=False, created_at=created))
        await s.commit()
    routines, _, _ = build(recording_bus, fake_memory)
    assert await routines.learned_checkin_time(user, weekend=False) == time(9, 35)  # median 10:05 − 30 min
    assert await routines.learned_checkin_time(user, weekend=True) == time(8, 30)  # no weekend data → default


async def test_morning_checkin_composes_with_items_and_reschedules(user, clock, recording_bus, fake_memory,
                                                                    fake_llm, channel, monkeypatch):
    clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))  # Mon 08:30 IST
    routines, loops, wakeups = build(recording_bus, fake_memory)
    await loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep with Jawahar",
                                           due_at=datetime(2026, 9, 28, 4, 30, tzinfo=UTC)))
    routine = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title=MORNING_TITLE))

    class Inbox:
        name = "inbox"

        async def items(self, user_id, start, end):
            return [BriefItem("2 unread from Acme recruiting", True)]

    routines_mod.register_brief_source(Inbox())
    seen = {}
    real_compose = Composer.compose

    async def spy(self, user_, intent, urgency, context="", **kw):
        seen["intent"] = intent
        return await real_compose(self, user_, intent, urgency, context, **kw)

    monkeypatch.setattr(Composer, "compose", spy)
    msg = ComposedMessage(send=True, messages=["Morning! Interview prep with Jawahar at 10."])
    fake_llm.push_structured(msg)
    await routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": routine.id})
    assert "Interview prep with Jawahar, due today 10:00 (in 1h 30m)" in seen["intent"]
    assert "2 unread from Acme recruiting" in seen["intent"]
    await deliver_pending(channel)
    assert any("Morning!" in str(s) for s in channel.sent)
    [w] = await wakeups.pending(user.id, WakeupKind.ROUTINE)
    assert w.due_at == datetime(2026, 9, 29, 3, 0, tzinfo=UTC)  # next day 08:30 IST


async def test_failing_brief_source_does_not_block(user, clock, recording_bus, fake_memory, fake_llm):
    clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))
    routines, loops, wakeups = build(recording_bus, fake_memory)

    class Broken:
        name = "broken"

        async def items(self, user_id, start, end):
            raise RuntimeError("provider down")

    routines_mod.register_brief_source(Broken())
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! Clear day today."]))
    await routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})
    assert len(await wakeups.pending(user.id, WakeupKind.ROUTINE)) == 1


async def test_existing_user_missing_wakeup_is_reseeded(user, clock, recording_bus, fake_memory):
    routines, loops, wakeups = build(recording_bus, fake_memory)
    # loop exists (e.g. migrated) but its wakeup is gone: reseed the wakeup only, no second loop
    await loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title=MORNING_TITLE))
    await routines.on_user_message(user)
    await routines.on_user_message(user)
    assert len([lp for lp in await loops.active(user.id) if lp.kind is LoopKind.ROUTINE]) == 1
    assert len(await wakeups.pending(user.id, WakeupKind.ROUTINE)) == 1


async def test_three_ignored_checkins_skip_sending_but_reschedule(user, clock, recording_bus, fake_memory,
                                                                   fake_llm):
    clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))
    routines, _, wakeups = build(recording_bus, fake_memory)
    async with Session() as s:
        for i in range(3):
            s.add(Message(user_id=user.id, role="assistant", content="Morning!", proactive=True,
                          event_id=f"proactive:morning:2026-09-2{i}:0",
                          created_at=datetime(2026, 9, 25 + i, 3, 0, tzinfo=UTC)))
        await s.commit()
    await routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})  # fake_llm empty would raise
    assert len(await wakeups.pending(user.id, WakeupKind.ROUTINE)) == 1


async def test_morning_hooks_run_before_the_brief_and_cannot_block_it(user, clock, recording_bus, fake_memory,
                                                                       fake_llm):
    clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))
    routines, loops, wakeups = build(recording_bus, fake_memory)
    routine = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title=MORNING_TITLE))
    seen = []

    async def bad(user_id):
        raise RuntimeError("boom")

    async def good(user_id):
        seen.append(user_id)

    routines_mod.clear_morning_hooks()
    routines_mod.register_morning_hook(bad)
    routines_mod.register_morning_hook(good)
    try:
        fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning!"]))
        await routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": routine.id})
    finally:
        routines_mod.clear_morning_hooks()
    assert seen == [user.id]
    assert await wakeups.pending(user.id, WakeupKind.ROUTINE)
