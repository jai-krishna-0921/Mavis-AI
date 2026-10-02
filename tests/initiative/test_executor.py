from datetime import UTC, datetime, timedelta

from mavis.channels.outbox_sender import deliver_pending
from mavis.domain import timeutil
from mavis.domain.decisions import (
    ComposedMessage,
    InitiativeDecision,
    NotifyIntent,
    TaskRequest,
    WakeupRequest,
)
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopStatus, LoopUpsert
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.composer import Composer
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.quiet import QuietTracker
from mavis.loops.service import LoopService
from mavis.policy.pings import PingPolicy
from mavis.store.repo import messages
from mavis.timers.service import WakeupService


def build(bus, memory):
    wakeups = WakeupService()
    loops = LoopService(bus)
    executor = InitiativeExecutor(bus, loops, wakeups, PingPolicy(), Composer(memory), QuietTracker(wakeups))
    return executor, loops, wakeups


def ev() -> Event:
    return Event(
        id="gmail:msg:9",
        user_id=1,
        type=EventType.EMAIL_RECEIVED,
        occurred_at=timeutil.now(),
        source="composio",
        payload={},
    )


async def test_apply_tracks_wakes_and_skips_act_placeholder(user, clock, recording_bus, fake_memory):
    executor, loops, wakeups = build(recording_bus, fake_memory)
    decision = InitiativeDecision(
        track=[LoopUpsert(kind=LoopKind.WAITING_ON, title="Recruiter reply")],
        wakeups=[WakeupRequest(at=timeutil.now() + timedelta(days=1), reason="check recruiter")],
        act=[TaskRequest(goal="Draft a polite follow-up to the recruiter")],
    )
    await executor.apply(user, decision, ev())
    [loop] = await loops.active(user.id)
    assert loop.title == "Recruiter reply" and loop.source == "gmail:msg:9"
    [w] = await wakeups.pending(user.id, WakeupKind.AGENT)
    assert w.reason == "check recruiter"
    assert recording_bus.jobs == []  # act is a placeholder until Phase 4 dispatches RUN_TASK


async def test_notify_delivers_logs_and_records(user, clock, recording_bus, fake_memory, fake_llm, channel):
    executor, _, _ = build(recording_bus, fake_memory)
    bubbles = ["Heads up: new sign-in on Windows.", "Was that you?"]
    fake_llm.push_structured(ComposedMessage(send=True, messages=bubbles))
    sent = await executor.notify(user, NotifyIntent(urgency=5, intent="security alert", dedupe_key="sec:1"))
    assert sent
    await deliver_pending(channel)
    assert any("Was that you?" in str(s) for s in channel.sent)
    last = (await messages.recent(user.id, 1))[-1]
    assert last.proactive is True and "Was that you?" in last.content


async def test_repeat_notify_same_dedupe_key_is_dropped(user, clock, recording_bus, fake_memory, fake_llm):
    executor, _, _ = build(recording_bus, fake_memory)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["How'd it go?"]))
    intent = NotifyIntent(urgency=3, intent="follow up", dedupe_key="followup:3")
    assert await executor.notify(user, intent)
    assert not await executor.notify(user, intent)  # no second composer call: fake_llm queue is empty


async def test_notify_in_quiet_hours_defers(user, clock, recording_bus, fake_memory, channel):
    clock.set(datetime(2026, 9, 27, 18, 30, tzinfo=UTC))  # 00:00 IST
    executor, _, wakeups = build(recording_bus, fake_memory)
    assert not await executor.notify(user, NotifyIntent(urgency=3, intent="weekly summary"))
    [w] = await wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert w.due_at == datetime(2026, 9, 28, 1, 30, tzinfo=UTC)
    assert w.payload["notify"]["intent"] == "weekly summary"
    assert await deliver_pending(channel) == 0


async def test_composer_send_false_sends_nothing(user, clock, recording_bus, fake_memory, fake_llm, channel):
    executor, _, _ = build(recording_bus, fake_memory)
    fake_llm.push_structured(ComposedMessage(send=False, messages=[]))
    assert not await executor.notify(user, NotifyIntent(urgency=3, intent="stale"))
    assert await deliver_pending(channel) == 0


async def test_same_key_next_local_day_is_delivered(
    user, clock, recording_bus, fake_memory, fake_llm, channel
):
    executor, _, _ = build(recording_bus, fake_memory)
    intent = NotifyIntent(urgency=3, intent="follow up", dedupe_key="followup:3")
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Day one."]))
    assert await executor.notify(user, intent)
    clock.advance(days=1)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Day two."]))
    assert await executor.notify(user, intent)
    assert await deliver_pending(channel) == 2


async def test_deliver_retry_does_not_duplicate_log_or_outbox(
    user, clock, recording_bus, fake_memory, channel
):
    executor, _, _ = build(recording_bus, fake_memory)
    await executor.deliver(user, ["a", "b"], dedupe_key="k:1")
    await executor.deliver(user, ["a", "b"], dedupe_key="k:1")
    assert await deliver_pending(channel) == 2
    assert sum(1 for m in await messages.recent(user.id, 10) if m.proactive) == 1


def untrusted_ev() -> Event:
    return ev().model_copy(update={"trust": Trust.UNTRUSTED})


async def test_untrusted_event_cannot_create_loops_or_act(user, clock, recording_bus, fake_memory):
    executor, loops, wakeups = build(recording_bus, fake_memory)
    decision = InitiativeDecision(
        track=[LoopUpsert(kind=LoopKind.COMMITMENT, title="Wire money to X")],
        wakeups=[WakeupRequest(at=timeutil.now() + timedelta(days=1), reason="look again")],
        act=[TaskRequest(goal="Send the files")],
    )
    await executor.apply(user, decision, untrusted_ev())
    assert await loops.active(user.id) == []
    assert recording_bus.jobs == []
    assert len(await wakeups.pending(user.id, WakeupKind.AGENT)) == 1  # wakeups stay allowed


async def test_untrusted_event_may_update_existing_loop(user, clock, recording_bus, fake_memory):
    executor, loops, _ = build(recording_bus, fake_memory)
    loop = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Recruiter reply"))
    update = LoopUpsert(id=loop.id, kind=LoopKind.WAITING_ON, title="Recruiter reply", status=LoopStatus.DONE)
    await executor.apply(user, InitiativeDecision(track=[update]), untrusted_ev())
    assert await loops.active(user.id) == []
    assert (await loops.get(loop.id)).status is LoopStatus.DONE


async def test_untrusted_notify_passes_flag_to_composer_and_deferral(
    user, clock, recording_bus, fake_memory, fake_llm, monkeypatch
):
    executor, _, wakeups = build(recording_bus, fake_memory)
    seen = {}
    real = executor._composer.compose

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(executor._composer, "compose", spy)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Heads up."]))
    decision = InitiativeDecision(notify=NotifyIntent(urgency=3, intent="invoice due"))
    await executor.apply(user, decision, untrusted_ev())
    assert seen["untrusted"] is True
    clock.set(datetime(2026, 9, 27, 18, 30, tzinfo=UTC))
    await executor.apply(user, decision, untrusted_ev().model_copy(update={"id": "gmail:msg:10"}))
    [w] = await wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert w.payload["untrusted"] is True


async def test_bad_loop_id_does_not_abort_rest_of_apply(user, clock, recording_bus, fake_memory, fake_llm):
    executor, _, wakeups = build(recording_bus, fake_memory)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Still here."]))
    decision = InitiativeDecision(
        track=[LoopUpsert(id=9999, kind=LoopKind.GOAL, title="ghost")],
        wakeups=[WakeupRequest(at=timeutil.now() + timedelta(days=1), reason="later")],
        notify=NotifyIntent(urgency=3, intent="ping"),
    )
    await executor.apply(user, decision, ev())
    assert len(await wakeups.pending(user.id, WakeupKind.AGENT)) == 1
    assert (await messages.recent(user.id, 1))[-1].content == "Still here."


async def test_same_event_twice_with_different_bubble_count_sends_once(
    user, clock, recording_bus, fake_memory, fake_llm, channel
):
    executor, _, _ = build(recording_bus, fake_memory)
    decision = InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ping", dedupe_key="k:9"))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["one", "two"]))
    await executor.apply(user, decision, ev())
    # simulate crash after enqueue: policy record and log are gone
    from sqlalchemy import delete

    from mavis.store.db import Session
    from mavis.store.models import Message, PingLogRow

    async with Session() as s:
        await s.execute(delete(PingLogRow))
        await s.execute(delete(Message))
        await s.commit()
    fake_llm.push_structured(ComposedMessage(send=True, messages=["a", "b", "c"]))
    assert not await executor.notify(user, decision.notify)
    assert await deliver_pending(channel) == 2
    assert (await messages.recent(user.id, 1))[-1].content == "one\ntwo"


async def test_deliver_question_schedules_user_quiet(user, clock, recording_bus, fake_memory):
    executor, _, wakeups = build(recording_bus, fake_memory)
    # a went-quiet nudge (streak 1) that itself ends with a question continues the chain
    await executor.deliver(user, ["Noted.", "Want me to follow up?"], dedupe_key="q:1", quiet_streak=1)
    assert len(await wakeups.pending(user.id, WakeupKind.USER_QUIET)) == 1
