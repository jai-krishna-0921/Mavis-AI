"""A1: a loop's trust is carried from its origin (the turn's taint, the event's trust), never inferred
from the shape of its source string. Every place the loop propagates (events, default wakeups, model
wakeups, brief items, recall) follows that value."""

from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain.decisions import (
    ComposedMessage,
    InitiativeDecision,
    NotifyIntent,
    TaskRequest,
    WakeupRequest,
)
from mavis.domain.events import Event, EventType, Provenance, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.memory import ExtractedEvent, Extraction, LoopDraft
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import routines as routines_mod
from mavis.initiative.planner import schedule_default_signals
from mavis.initiative.routines import MORNING_ROUTINE
from mavis.initiative.wiring import build_initiative
from mavis.loops.service import LoopService, loops_from_extraction
from mavis.memory.recall import render_loop

DUE = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)  # Mon 10:00 IST
# Source strings of every shape the system produces. None of them may decide trust.
SOURCE_SHAPES = ["tg:update:9", "cli:7c1e", "chat:abc", "local:1", "gmail:msg-1", "web:https://x.test",
                 "task:5", "wakeup:32", ""]


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def _extraction(title: str = "Send the deck", event: str = "Dinner with Priya") -> Extraction:
    return Extraction(
        loops=[LoopDraft(kind="commitment", title=title, due_at=datetime(2026, 9, 28, 10, 0))],
        events=[ExtractedEvent(title=event, starts_at=datetime(2026, 9, 28, 20, 0), importance=4)],
    )


@pytest.fixture(autouse=True)
def _no_sources():
    routines_mod.clear_brief_sources()
    yield
    routines_mod.clear_brief_sources()


@pytest.mark.parametrize("source_ref", SOURCE_SHAPES)
async def test_untainted_user_turn_yields_trusted_loops_whatever_the_source(user, recording_bus, clock,
                                                                            source_ref):
    svc = LoopService(recording_bus)
    prov = Provenance(source_ref=source_ref, trust=Trust.USER, conversation=True)
    await loops_from_extraction(svc, user.id, _extraction(), prov)
    loops = await svc.active(user.id)
    assert len(loops) == 2
    assert all(lp.trust is Trust.USER and lp.trusted for lp in loops)
    assert all(lp.origin is LoopOrigin.CONVERSATION for lp in loops)
    assert all(e.trust is not Trust.UNTRUSTED for e in recording_bus.take())


@pytest.mark.parametrize("source_ref", SOURCE_SHAPES)
async def test_tainted_turn_yields_untrusted_loops_whatever_the_source(user, recording_bus, clock,
                                                                       source_ref):
    svc = LoopService(recording_bus)
    prov = Provenance(source_ref=source_ref, trust=Trust.UNTRUSTED, conversation=True)
    await loops_from_extraction(svc, user.id, _extraction("Register for the meetup"), prov)
    loops = await svc.active(user.id)
    assert len(loops) == 2 and not any(lp.trusted for lp in loops)
    events = recording_bus.take()
    assert events and all(e.type is EventType.LOOP_CREATED and e.trust is Trust.UNTRUSTED for e in events)


@pytest.mark.parametrize("trust", [Trust.USER, Trust.UNTRUSTED, Trust.SYSTEM])
@pytest.mark.parametrize("source_ref", ["tg:update:3", "gmail:msg-9", ""])
async def test_ingested_documents_never_create_loops(user, recording_bus, clock, trust, source_ref):
    """Third-party documents (first sync, signals) are not a conversation: no loops, whatever the id."""
    svc = LoopService(recording_bus)
    await loops_from_extraction(svc, user.id, _extraction(), Provenance(source_ref=source_ref, trust=trust))
    assert await svc.active(user.id) == []
    assert recording_bus.take() == []


@pytest.mark.parametrize("trust", [Trust.SYSTEM, Trust.UNTRUSTED])
async def test_only_user_typed_turns_are_trusted(user, recording_bus, clock, trust):
    svc = LoopService(recording_bus)
    await loops_from_extraction(svc, user.id, _extraction(),
                                Provenance(source_ref="tg:update:1", trust=trust, conversation=True))
    assert not any(lp.trusted for lp in await svc.active(user.id))


async def test_learn_hands_its_trust_to_the_hooks(user, memory, recording_bus, clock, fake_llm):
    svc = LoopService(recording_bus)

    async def hook(uid, extraction, prov):
        await loops_from_extraction(svc, uid, extraction, prov)

    memory.on_extraction.append(hook)
    fake_llm.push_structured(_extraction("Book the venue", "Venue walkthrough"))
    await memory.learn(user.id, "Mavis: (email summary)\nUser: ok book the venue", source_ref="tg:update:42",
                       trust=Trust.UNTRUSTED)
    fake_llm.push_structured(_extraction("Call the plumber", "Plumber visit"))
    await memory.learn(user.id, "User: I'll call the plumber", source_ref="tg:update:43", trust=Trust.USER)
    by_title = {lp.title: lp for lp in await svc.active(user.id)}
    assert not by_title["Book the venue"].trusted
    assert by_title["Call the plumber"].trusted


async def test_untrusted_merge_taints_a_trusted_loop_but_status_changes_do_not(user, recording_bus, clock):
    svc = LoopService(recording_bus)
    mine = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Renew passport", due_at=DUE,
                                                trust=Trust.USER))
    assert mine.trusted
    closed = await svc.close(mine.id, LoopStatus.AWAITING_REPLY)
    assert closed is not None and closed.trusted
    gym = LoopUpsert(kind=LoopKind.COMMITMENT, title="Gym plan", trust=Trust.USER)
    other = await svc.upsert(user.id, gym)
    merged = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="gym plan",
                                                  entities=["Coach"], trust=Trust.UNTRUSTED))
    assert merged.id == other.id and not merged.trusted
    again = await svc.upsert(user.id, gym)
    assert not again.trusted  # taint is sticky: restating it does not launder the third-party content


@pytest.mark.parametrize("trust,untrusted", [(Trust.USER, False), (Trust.SYSTEM, False),
                                             (Trust.UNTRUSTED, True)])
async def test_default_wakeups_follow_the_loop(user, recording_bus, clock, trust, untrusted):
    svc = LoopService(recording_bus)
    loop = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Demo day", due_at=DUE,
                                                importance=5, trust=trust))
    init = build_initiative(recording_bus, None, embed=no_embed)
    await schedule_default_signals(init.wakeups, loop)
    pending = await init.wakeups.pending(user.id)
    assert {w.kind for w in pending} == {WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED}
    assert all(bool((w.payload or {}).get("untrusted")) is untrusted for w in pending)


async def test_tainted_turn_loop_created_plans_untrusted_wakeups(user, clock, recording_bus, fake_memory,
                                                                fake_llm):
    """End to end: LEARN on a tainted turn -> loop -> LOOP_CREATED -> default and model wakeups, all
    untrusted; the reasoner cannot start work or track new loops from it."""
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    await loops_from_extraction(init.loops, user.id, Extraction(loops=[
        LoopDraft(kind="commitment", title="Claim the conference voucher",
                  due_at=datetime(2026, 9, 28, 10, 0), importance=5)]),
        Provenance(source_ref="cli:abc", trust=Trust.UNTRUSTED, conversation=True))
    [created] = recording_bus.take()
    assert created.trust is Trust.UNTRUSTED
    loop_id = created.payload["id"]
    fake_llm.push_structured(InitiativeDecision(
        wakeups=[WakeupRequest(at=DUE - timedelta(hours=3), reason="check the voucher", loop_id=loop_id)],
        act=[TaskRequest(goal="claim it")],
        track=[LoopUpsert(kind=LoopKind.COMMITMENT, title="Brand new loop")]))
    await init.handler.handle(created)
    pending = await init.wakeups.pending(user.id)
    assert pending and all((w.payload or {}).get("untrusted") is True for w in pending)
    assert [lp.title for lp in await init.loops.active(user.id)] == ["Claim the conference voucher"]


async def test_model_wakeup_for_an_untrusted_loop_is_untrusted_on_a_trusted_event(user, clock, recording_bus,
                                                                                 fake_memory, fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WATCH, title="Invoice from vendor",
                                                       trust=Trust.UNTRUSTED))
    recording_bus.take()
    event = Event(id="wakeup:77", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
                  payload={"kind": "agent", "reason": "check on invoice", "loop_id": loop.id},
                  trust=Trust.SYSTEM)
    request = WakeupRequest(at=clock.t + timedelta(hours=5), reason="invoice again", loop_id=loop.id)
    decision = InitiativeDecision(wakeups=[request])
    await init.executor.apply(user, decision, event)
    [w] = await init.wakeups.pending(user.id)
    assert w.payload.get("untrusted") is True


async def test_reasoner_tracked_loops_are_untrusted_and_cannot_claim_trust(user, clock, recording_bus,
                                                                           fake_memory):
    """A model-supplied trust/source/origin is ignored: a clean run on a trusted event writes SYSTEM trust
    (not the USER the model claimed), and a run whose prompt carried untrusted content writes untrusted."""
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    event = Event(id="wakeup:5", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
                  payload={"kind": "agent", "reason": "r"}, trust=Trust.SYSTEM)
    upsert = LoopUpsert(kind=LoopKind.COMMITMENT, title="Reply to the security alert", trust=Trust.USER,
                        source="User agreed", origin=LoopOrigin.CONVERSATION)
    await init.executor.apply(user, InitiativeDecision(track=[upsert]), event)
    [loop] = await init.loops.active(user.id)
    assert loop.trust is Trust.SYSTEM and loop.origin is LoopOrigin.REASONER and loop.source == "wakeup:5"
    other = LoopUpsert(kind=LoopKind.COMMITMENT, title="Pay the invoice", trust=Trust.USER)
    await init.executor.apply(user, InitiativeDecision(track=[other], tainted=True), event)
    assert not next(lp for lp in await init.loops.active(user.id) if lp.title == "Pay the invoice").trusted


async def test_untrusted_event_update_taints_the_loop(user, clock, recording_bus, fake_memory):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from landlord",
                                                       trust=Trust.USER))
    event = Event(id="gmail:msg:z", user_id=user.id, type=EventType.EMAIL_RECEIVED, occurred_at=clock.t,
                  source="composio", payload={}, trust=Trust.UNTRUSTED)
    update = LoopUpsert(id=loop.id, kind=LoopKind.WAITING_ON, title="x", entities=["Landlord Ltd"])
    await init.executor.apply(user, InitiativeDecision(track=[update]), event)
    after = await init.loops.get(loop.id)
    assert after is not None and not after.trusted and after.title == "Reply from landlord"


@pytest.mark.parametrize("trust,wrapped", [(Trust.USER, False), (Trust.UNTRUSTED, True)])
async def test_brief_items_follow_the_loop(user, clock, recording_bus, fake_memory, fake_llm, monkeypatch,
                                           trust, wrapped):
    clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))  # Mon 08:30 IST
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Quarterly review",
                                                due_at=DUE, trust=trust))
    seen: dict = {}

    async def spy(self, user_, intent, urgency, context="", untrusted=False, **kw):
        seen["intent"], seen["untrusted"] = intent, untrusted
        return ComposedMessage(send=False, messages=[])

    from mavis.initiative.composer import Composer
    monkeypatch.setattr(Composer, "compose", spy)
    await init.routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})
    assert "Quarterly review" in seen["intent"]
    assert ("<untrusted" in seen["intent"]) is wrapped
    assert seen["untrusted"] is wrapped


@pytest.mark.parametrize("trust,wrapped", [(Trust.USER, False), (Trust.SYSTEM, False),
                                           (Trust.UNTRUSTED, True)])
def test_recall_marks_untrusted_loops(trust, wrapped):
    from mavis.domain.loops import Loop

    loop = Loop(id=1, user_id=1, kind=LoopKind.COMMITMENT, title="Pay the deposit", trust=trust)
    line = render_loop(loop, "Asia/Kolkata")
    assert "Pay the deposit" in line
    assert ("<untrusted" in line) is wrapped


@pytest.mark.parametrize("origin,suppressed", [(LoopOrigin.CONVERSATION, True), (LoopOrigin.REASONER, False),
                                               (LoopOrigin.FEEDBACK, False), (LoopOrigin.UNKNOWN, False)])
@pytest.mark.parametrize("source", ["tg:update:1", "cli:9", "wakeup:3", ""])
def test_post_turn_quieting_follows_origin_not_source(origin, suppressed, source):
    from mavis.initiative.handler import _quiet_after_turn

    event = Event(id="loop:1:created", user_id=1, type=EventType.LOOP_CREATED, occurred_at=DUE,
                  source="agent", payload={"id": 1, "source": source, "origin": origin.value})
    decision = InitiativeDecision(notify=NotifyIntent(urgency=3, intent="nudge"))
    out = _quiet_after_turn(event, decision)
    assert (out.notify is None) is suppressed


# --- fix round 1, I2: a wakeup's trust is decided when it fires, from its loop's CURRENT trust --------


class _Leader:
    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None


@pytest.mark.parametrize("kind", [WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED, WakeupKind.AGENT])
@pytest.mark.parametrize("loop_trust,flag,expected", [
    (Trust.USER, None, Trust.SYSTEM),
    (Trust.SYSTEM, None, Trust.SYSTEM),
    (Trust.UNTRUSTED, None, Trust.UNTRUSTED),          # e.g. a legacy loop the migration marked untrusted
    (Trust.USER, {"untrusted": True}, Trust.UNTRUSTED),  # the flag still counts: least trusted wins
])
async def test_fired_wakeup_combines_its_flag_with_the_loops_trust(user, clock, recording_bus, kind,
                                                                   loop_trust, flag, expected):
    from mavis.timers.runner import TimerRunner
    from mavis.timers.service import WakeupService

    loops = LoopService(recording_bus)
    loop = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Board prep",
                                                  trust=loop_trust))
    recording_bus.take()
    await WakeupService().wake_me(user.id, clock.t + timedelta(minutes=1), "r", loop.id, kind, payload=flag,
                                  scale=False)
    clock.set(clock.t + timedelta(minutes=2))
    await TimerRunner(recording_bus, WakeupService(), _Leader(), 0.01, loops=loops).tick()
    [event] = [e for e in recording_bus.take() if e.type is not EventType.LOOP_UPDATED]
    assert event.trust is expected
    assert bool(event.payload.get("untrusted")) is (expected is Trust.UNTRUSTED)


async def test_wakeup_scheduled_trusted_fires_untrusted_after_its_loop_was_tainted(user, clock,
                                                                                  recording_bus):
    from mavis.timers.runner import TimerRunner
    from mavis.timers.service import WakeupService

    loops = LoopService(recording_bus)
    loop = await loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from Ana",
                                                  trust=Trust.USER))
    await WakeupService().wake_me(user.id, clock.t + timedelta(minutes=1), "check", loop.id, WakeupKind.AGENT,
                                  scale=False)
    await loops.upsert(user.id, LoopUpsert(id=loop.id, kind=LoopKind.WAITING_ON, title="Reply from Ana",
                                           entities=["Ana Corp"], trust=Trust.UNTRUSTED))
    recording_bus.take()
    clock.set(clock.t + timedelta(minutes=2))
    await TimerRunner(recording_bus, WakeupService(), _Leader(), 0.01, loops=loops).tick()
    [event] = [e for e in recording_bus.take() if e.type is EventType.WAKEUP]
    assert event.trust is Trust.UNTRUSTED


# --- fix round 1, I3: reasoner updates are partial and carry the run's real taint ----------------------


def _wakeup(user_id, clock, trust=Trust.SYSTEM):
    return Event(id="wakeup:41", user_id=user_id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
                 payload={"kind": "agent", "reason": "r"}, trust=trust)


@pytest.mark.parametrize("status", [LoopStatus.AWAITING_REPLY, LoopStatus.DONE, LoopStatus.DROPPED])
@pytest.mark.parametrize("tainted_run", [False, True])
async def test_status_only_reasoner_update_never_touches_content_or_trust(user, clock, recording_bus,
                                                                          fake_memory, status, tainted_run):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Renew passport",
                                                       due_at=DUE, importance=5, trust=Trust.USER))
    update = LoopUpsert.model_validate({"id": loop.id, "status": status.value})  # what the model sends
    await init.executor.apply(user, InitiativeDecision(track=[update], tainted=tainted_run),
                              _wakeup(user.id, clock))
    after = await init.loops.get(loop.id)
    # H5: a status change without code evidence is only a note, so the loop stays OPEN
    assert after.status is LoopStatus.OPEN and after.trusted
    assert (after.kind, after.title, after.due_at, after.importance) == (loop.kind, loop.title, DUE, 5)


@pytest.mark.parametrize("tainted_run,event_trust,trusted_after", [
    (False, Trust.SYSTEM, True),      # a clean run on a trusted event keeps the loop trusted
    (True, Trust.SYSTEM, False),      # its prompt carried untrusted content: the change taints
    (False, Trust.UNTRUSTED, False),  # (an untrusted event may only touch status/entities/watch)
])
async def test_reasoner_content_change_carries_the_runs_taint(user, clock, recording_bus, fake_memory,
                                                              tainted_run, event_trust, trusted_after):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from Ana",
                                                       trust=Trust.USER))
    update = LoopUpsert.model_validate({"id": loop.id, "entities": ["Ana Ltd"]})
    await init.executor.apply(user, InitiativeDecision(track=[update], tainted=tainted_run),
                              _wakeup(user.id, clock, event_trust))
    after = await init.loops.get(loop.id)
    assert after.entities == ["Ana Ltd"] and after.trusted is trusted_after


@pytest.mark.parametrize("tainted_run", [False, True])
async def test_reasoner_created_loop_trust_follows_the_run(user, clock, recording_bus, fake_memory,
                                                           tainted_run):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    upsert = LoopUpsert(kind=LoopKind.GOAL, title="Plan the offsite")
    decision = InitiativeDecision(track=[upsert], tainted=tainted_run)
    await init.executor.apply(user, decision, _wakeup(user.id, clock))
    [created] = await init.loops.active(user.id)
    assert created.trusted is (not tainted_run) and created.origin is LoopOrigin.REASONER


def test_a_new_loop_needs_kind_and_title():
    with pytest.raises(ValueError):
        LoopUpsert.model_validate({"title": "no kind"})
    with pytest.raises(ValueError):
        LoopUpsert.model_validate({"kind": "GOAL"})
    assert LoopUpsert.model_validate({"id": 3, "status": "DONE"}).title is None


async def test_reasoner_marks_its_decision_tainted_from_its_actual_inputs(user, clock, fake_memory,
                                                                          monkeypatch):
    from mavis.domain.loops import Loop
    from mavis.domain.memory import RecallContext
    from mavis.domain.messages import TAINT_SUFFIX, Role
    from mavis.initiative.filters import FilterResult
    from mavis.initiative.reasoner import Reasoner
    from mavis.llm import models as llm
    from mavis.policy.pings import PingPolicy
    from mavis.store.repo import messages

    async def fake(schema, system, user_msg, **kw):
        return InitiativeDecision(ignore_reason="t", tainted=True)  # a model-set flag is ignored

    monkeypatch.setattr(llm, "structured", fake)
    reasoner = Reasoner(fake_memory, PingPolicy())
    ev = _wakeup(user.id, clock)
    clean = FilterResult(drop=False, relevance=0.5, summary="s")
    assert (await reasoner.decide(user, ev, clean)).tainted is False
    untrusted_loop = Loop(id=1, user_id=user.id, kind=LoopKind.WATCH, title="x", trust=Trust.UNTRUSTED)
    with_loop = FilterResult(drop=False, relevance=0.5, summary="s", matched_loops=[untrusted_loop])
    assert (await reasoner.decide(user, ev, with_loop)).tainted is True
    assert (await reasoner.decide(user, _wakeup(user.id, clock, Trust.UNTRUSTED), clean)).tainted is True
    fake_memory.recall_result = RecallContext(episodes=["<x>"], untrusted=True)
    assert (await reasoner.decide(user, ev, clean)).tainted is True
    fake_memory.recall_result = RecallContext()
    await messages.log(user.id, Role.ASSISTANT, "summary of an email", event_id=f"say:1{TAINT_SUFFIX}")
    assert (await reasoner.decide(user, ev, clean)).tainted is True


@pytest.mark.parametrize("tainted_run,expected", [(False, None), (True, True)])
async def test_scheduled_wakeup_follows_the_runs_taint_on_a_trusted_event(user, clock, recording_bus,
                                                                         fake_memory, tainted_run, expected):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.GOAL, title="Get fit", trust=Trust.USER))
    request = WakeupRequest(at=clock.t + timedelta(hours=2), reason="check the thing", loop_id=loop.id)
    await init.executor.apply(user, InitiativeDecision(wakeups=[request], tainted=tainted_run),
                              _wakeup(user.id, clock))
    [w] = await init.wakeups.pending(user.id)
    assert (w.payload or {}).get("untrusted") is expected
