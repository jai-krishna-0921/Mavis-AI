from datetime import UTC, datetime, timedelta

from mavis.channels.outbox_sender import deliver_pending
from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, WakeupRequest
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopStatus, LoopUpsert
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.wiring import build_initiative

DUE = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def llm_down(monkeypatch):
    async def boom(*args, **kwargs):
        raise LLMError("model unavailable")

    monkeypatch.setattr(Reasoner, "decide", boom)


async def test_promo_email_never_reaches_reasoner(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    called = {"n": 0}

    async def counting(*a, **k):
        called["n"] += 1
        return InitiativeDecision()

    monkeypatch.setattr(Reasoner, "decide", counting)
    await init.handler.handle(Event(id="gmail:msg:p", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                                    occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                                    payload={"from": "deals@shop.com", "subject": "Sale",
                                             "labels": ["CATEGORY_PROMOTIONS"]}))
    assert called["n"] == 0


async def test_loop_created_llm_failure_schedules_defaults(user, clock, recording_bus, fake_memory,
                                                           monkeypatch):
    init = build(recording_bus, fake_memory)
    llm_down(monkeypatch)
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep", due_at=DUE,
                                                importance=5))
    [created] = recording_bus.take()
    await init.handler.handle(created)
    pending = {w.kind: w for w in await init.wakeups.pending(user.id)}
    assert pending[WakeupKind.EVENT_STARTING].due_at == DUE - timedelta(hours=1)
    assert pending[WakeupKind.EVENT_ENDED].due_at == DUE + timedelta(hours=2)


async def test_loop_created_with_reasoner_wakeups_keeps_defaults(user, clock, recording_bus, fake_memory,
                                                                 fake_llm):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist", due_at=DUE,
                                                       importance=3))
    [created] = recording_bus.take()
    request = WakeupRequest(at=DUE - timedelta(hours=3), reason="remind dentist", loop_id=loop.id)
    fake_llm.push_structured(InitiativeDecision(wakeups=[request]))
    await init.handler.handle(created)
    # QA F3: the default follow-up is always scheduled; the model's own wakeup is kept alongside
    assert [w.kind for w in await init.wakeups.pending(user.id)] == [WakeupKind.AGENT, WakeupKind.EVENT_ENDED]


async def test_routine_loop_created_is_ignored(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title="Morning check-in"))
    [created] = recording_bus.take()
    await init.handler.handle(created)  # fake_llm empty: any LLM call would raise
    assert await init.wakeups.pending(user.id) == []


async def test_loop_done_cancels_its_wakeups(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    llm_down(monkeypatch)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Call", due_at=DUE,
                                                       importance=5))
    await init.handler.handle(recording_bus.take()[0])
    await init.loops.close(loop.id, LoopStatus.DROPPED)
    [updated] = recording_bus.take()
    await init.handler.handle(updated)
    assert await init.wakeups.pending(user.id) == []


async def test_event_ended_llm_failure_still_follows_up(user, clock, recording_bus, fake_memory, fake_llm,
                                                        channel, monkeypatch):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview prep",
                                                       due_at=DUE, importance=5))
    recording_bus.take()
    llm_down(monkeypatch)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["How'd the interview prep go?"]))
    await init.handler.handle(Event(id="wakeup:99", user_id=user.id, type=EventType.EVENT_ENDED,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"wakeup_id": 99, "kind": "event_ended", "loop_id": loop.id,
                                             "reason": "Follow up"}))
    await deliver_pending(channel)
    assert any("How'd the interview prep go?" in str(s) for s in channel.sent)
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY  # until the user answers


async def test_deferred_wakeup_goes_straight_to_notify(user, clock, recording_bus, fake_memory, fake_llm,
                                                       channel):
    init = build(recording_bus, fake_memory)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! About that weekly summary..."]))
    await init.handler.handle(Event(id="wakeup:5", user_id=user.id, type=EventType.WAKEUP,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"wakeup_id": 5, "kind": "deferred",
                                             "notify": {"urgency": 3, "intent": "weekly summary"}}))
    await deliver_pending(channel)
    assert any("weekly summary" in str(s) for s in channel.sent)


async def test_user_quiet_skipped_when_user_replied(user, clock, recording_bus, fake_memory):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    asked_at = timeutil.now() - timedelta(hours=5)
    await messages.log(user.id, Role.USER, "sorry, was busy")
    await init.handler.handle(Event(id="wakeup:6", user_id=user.id, type=EventType.USER_QUIET,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"wakeup_id": 6, "kind": "user_quiet",
                                             "asked_at": asked_at.isoformat(),
                                             "question": "What's on your plate?", "streak": 0}))
    # fake_llm is empty: reaching the reasoner/composer would raise


async def test_deferred_wakeup_passes_untrusted_flag(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    seen = {}

    async def spy(user_, intent, context="", quiet_streak=0, untrusted=False, **kw):
        seen["untrusted"] = untrusted
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(Event(id="wakeup:7", user_id=user.id, type=EventType.WAKEUP,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"wakeup_id": 7, "kind": "deferred", "untrusted": True,
                                             "notify": {"urgency": 3, "intent": "check inbox"}}))
    assert seen == {"untrusted": True}
