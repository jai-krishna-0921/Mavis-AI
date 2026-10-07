"""H5: the reasoner's own writes never feed it, its closure claims are notes, and silence closes nothing."""

from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert, WatchSpec
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.wiring import build_initiative

DUE = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)
CLOSING = [LoopStatus.DONE, LoopStatus.DROPPED, LoopStatus.EXPIRED]


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def count_reasoner(monkeypatch) -> dict:
    called = {"n": 0}

    async def counting(*a, **k):
        called["n"] += 1
        return InitiativeDecision()

    monkeypatch.setattr(Reasoner, "decide", counting)
    return called


def timer_event(user, etype: EventType, eid: str, **payload) -> Event:
    return Event(id=eid, user_id=user.id, type=etype, occurred_at=timeutil.now(), source="timer",
                 trust=Trust.SYSTEM, payload={"wakeup_id": 1, "reason": "look again", **payload})


# reasoner-origin LOOP_CREATED is not a signal ----------------------------------------------------

@pytest.mark.parametrize(("kind", "title", "due"), [
    (LoopKind.WAITING_ON, "Reply about the offer to help", None),
    (LoopKind.COMMITMENT, "Review outcome of the workshop", DUE),
    (LoopKind.CONCERN, "Approval still pending for Thursday's slot", DUE + timedelta(days=1)),
])
async def test_reasoner_track_does_not_retrigger_the_reasoner(user, clock, recording_bus, fake_memory,
                                                              monkeypatch, kind, title, due):
    init = build(recording_bus, fake_memory)
    called = count_reasoner(monkeypatch)
    await init.loops.upsert(user.id, LoopUpsert(kind=kind, title=title, due_at=due, trust=Trust.SYSTEM,
                                                origin=LoopOrigin.REASONER))
    [created] = recording_bus.take()
    await init.handler.handle(created)
    assert called["n"] == 0
    assert await init.wakeups.pending(user.id) == []  # no derived prep / follow-up for a belief either


@pytest.mark.parametrize("origin", [LoopOrigin.CONVERSATION, LoopOrigin.UNKNOWN, LoopOrigin.FEEDBACK])
async def test_other_origins_still_reach_the_reasoner(user, clock, recording_bus, fake_memory, monkeypatch,
                                                     origin):
    init = build(recording_bus, fake_memory)
    called = count_reasoner(monkeypatch)
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist", due_at=DUE,
                                                origin=origin))
    await init.handler.handle(recording_bus.take()[0])
    assert called["n"] == 1


async def test_reasoner_track_inside_a_run_does_not_echo(user, clock, recording_bus, fake_memory, fake_llm):
    """The run's own `track` publishes LOOP_CREATED; handling it costs no LLM call and sends nothing."""
    init = build(recording_bus, fake_memory)
    fake_llm.push_structured(InitiativeDecision(
        notify=NotifyIntent(urgency=3, intent="ask about the parcel"),
        track=[LoopUpsert(kind=LoopKind.WAITING_ON, title="Parcel pickup answer")]))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Did the parcel turn up?"]))
    await init.handler.handle(timer_event(user, EventType.USER_QUIET, "wakeup:31",
                                          kind="user_quiet", asked_at=timeutil.now().isoformat()))
    echoes = [e for e in recording_bus.take() if e.type is EventType.LOOP_CREATED]
    assert echoes and echoes[0].payload["origin"] == LoopOrigin.REASONER.value
    for e in echoes:
        await init.handler.handle(e)  # fake_llm is empty: any reasoner or composer call would raise
    assert len(fake_llm.structured_calls) == 2


# closure claims are notes -----------------------------------------------------------------------

@pytest.mark.parametrize("status", CLOSING)
@pytest.mark.parametrize(("etype", "kind"), [
    (EventType.WAKEUP, "agent"),
    (EventType.EVENT_ENDED, "event_ended"),
    (EventType.EVENT_STARTING, "event_starting"),
    (EventType.USER_QUIET, "user_quiet"),
])
async def test_reasoner_cannot_close_a_loop_without_evidence(user, clock, recording_bus, fake_memory,
                                                             fake_llm, status, etype, kind):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Send the lease scan",
                                                       due_at=timeutil.now() + timedelta(days=1),
                                                       importance=3))
    recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(track=[LoopUpsert(id=loop.id, status=status, importance=4)]))
    payload = {"kind": kind, "loop_id": loop.id}
    if etype is EventType.USER_QUIET:  # a question this loop depends on, so the nudge is owed
        payload.update(asked_at=timeutil.now().isoformat(), subject=f"loop:{loop.id}")
    await init.handler.handle(timer_event(user, etype, f"wakeup:{kind}:{status}", **payload))
    after = await init.loops.get(loop.id)
    assert after.status is LoopStatus.OPEN
    assert after.importance == 4  # the rest of the update still applies


@pytest.mark.parametrize("status", CLOSING)
async def test_reasoner_cannot_create_an_already_closed_loop(user, clock, recording_bus, fake_memory, status):
    init = build(recording_bus, fake_memory)
    decision = InitiativeDecision(track=[LoopUpsert(kind=LoopKind.COMMITMENT, title="Blocked the morning",
                                                    status=status)])
    await init.executor.apply(user, decision, timer_event(user, EventType.WAKEUP, "wakeup:77", kind="agent"))
    assert await init.loops.active(user.id) == []
    assert recording_bus.take() == []


@pytest.mark.parametrize(("sender", "subject"), [
    ("landlord@flats.example", "Signed lease attached"),
    ("hr@company.example", "Your reimbursement was paid"),
])
async def test_watched_external_signal_is_evidence(user, clock, recording_bus, fake_memory, fake_llm,
                                                   sender, subject):
    """A code-matched external signal (the loop's watch) is evidence: the reasoner may close that loop."""
    init = build(recording_bus, fake_memory)
    domain = sender.split("@")[1]
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title=f"Reply from {domain}",
                                                       watch=WatchSpec(from_contains=domain)))
    other = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Plumber quote"))
    recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(track=[LoopUpsert(id=loop.id, status=LoopStatus.DONE),
                                                       LoopUpsert(id=other.id, status=LoopStatus.DONE)]))
    await init.handler.handle(Event(id=f"gmail:{subject}", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                                    occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                                    payload={"from": sender, "subject": subject, "snippet": "see attached"}))
    assert (await init.loops.get(loop.id)).status is LoopStatus.DONE
    assert (await init.loops.get(other.id)).status is LoopStatus.OPEN  # not matched: no evidence for it


# silence never yields DONE ----------------------------------------------------------------------

@pytest.mark.parametrize("decision", [
    InitiativeDecision(ignore_reason="nothing to say"),
    InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ask how it went")),  # composer drops it
])
async def test_ended_event_without_a_delivered_follow_up_leaves_the_loop_open(
        user, clock, recording_bus, fake_memory, fake_llm, decision):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Pick up the keys",
                                                       due_at=timeutil.now() - timedelta(hours=2)))
    recording_bus.take()
    fake_llm.push_structured(decision)
    if decision.notify is not None:
        fake_llm.push_structured(ComposedMessage(send=False))
    await init.handler.handle(timer_event(user, EventType.EVENT_ENDED, "wakeup:300", kind="event_ended",
                                          loop_id=loop.id))
    assert (await init.loops.get(loop.id)).status is LoopStatus.OPEN


async def test_stale_deferred_follow_up_leaves_the_loop_open(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Gym induction",
                                                       due_at=timeutil.now() - timedelta(days=2)))
    recording_bus.take()
    origin = {"kind": "event_ended", "loop_id": loop.id,
              "valid_until": (timeutil.now() - timedelta(hours=1)).isoformat()}
    await init.handler.handle(Event(
        id="wakeup:301", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer",
        payload={"wakeup_id": 301, "kind": WakeupKind.DEFERRED.value, "loop_id": loop.id, "origin": origin,
                 "notify": {"urgency": 3, "intent": "ask how it went"}}))
    assert (await init.loops.get(loop.id)).status is LoopStatus.OPEN
