"""H5: every AGENT wakeup is bound to an explicit, existing subject; no detach, no token match, no chains."""

from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, WakeupRequest
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.tasks import ApprovalStatus, TaskStatus
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.subjects import Subject, SubjectKind, event_subject, resolve
from mavis.initiative.wiring import build_initiative
from mavis.store.repo import approvals, attention, tasks, users
from mavis.timers.runner import wakeup_event


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def later(hours: int = 6):
    return timeutil.now() + timedelta(hours=hours)


def signal(user, etype: EventType, eid: str, source: str = "timer", **payload) -> Event:
    return Event(id=eid, user_id=user.id, type=etype, occurred_at=timeutil.now(), source=source,
                 trust=Trust.SYSTEM, payload=payload)


async def agent_wakeups(init, user):
    return await init.wakeups.pending(user.id, WakeupKind.AGENT)


async def make_loop(init, user, title="Send the tax forms", **kw):
    data = {"kind": LoopKind.COMMITMENT, "title": title, "due_at": later(30), "importance": 3,
            "trust": Trust.SYSTEM, "origin": LoopOrigin.CONVERSATION, **kw}
    return await init.loops.upsert(user.id, LoopUpsert(**data))


async def make_approval(user, status=ApprovalStatus.PENDING):
    aid = await approvals.create(user.id, None, "mail_send", {"to": ["a@b.example"]}, "Send note to A",
                                 later(48))
    if status is not ApprovalStatus.PENDING:
        await approvals.set_status(aid, status)
    return aid


async def make_task(user, status=TaskStatus.RUNNING):
    tid = await tasks.create(user.id, "Compare three phone plans")
    await tasks.set_status(tid, status)
    return tid


async def make_observation(user, sender="Acme Insurance", domain="acme.example"):
    obs, _ = await attention.insert_pending(user.id, f"m-{domain}", thread_id="t", origin="live",
                                            sender_domain=domain, sender_name=sender,
                                            received_at=timeutil.now(), payload={})
    return obs.id


# unbound wakeups are rejected --------------------------------------------------------------------

@pytest.mark.parametrize(("etype", "payload"), [
    (EventType.USER_QUIET, {"kind": "user_quiet", "asked_at": "2026-09-27T07:00:00+00:00"}),
    (EventType.WAKEUP, {"kind": "agent", "reason": "look again"}),
    (EventType.CONNECTION_CHANGED, {"toolkit": "gmail", "state": "active"}),
])
@pytest.mark.parametrize("reason", ["check in later", "see if they replied to the offer",
                                    "morning check on progress"])
async def test_wakeup_without_a_subject_is_rejected(user, clock, recording_bus, fake_memory, etype,
                                                    payload, reason):
    init = build(recording_bus, fake_memory)
    await init.executor.apply(user, InitiativeDecision(wakeups=[WakeupRequest(at=later(), reason=reason)]),
                              signal(user, etype, f"ev:{etype}:{reason}", **payload))
    assert await agent_wakeups(init, user) == []


@pytest.mark.parametrize("reason", ["Check in on the morning plan", "check the dentist appointment status",
                                    "Review how the tax forms went"])
async def test_reason_words_never_bind_a_wakeup(user, clock, recording_bus, fake_memory, reason):
    """No title-token match: a reason naming a loop's words binds nothing without its id."""
    init = build(recording_bus, fake_memory)
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title="Morning check-in",
                                                trust=Trust.SYSTEM, origin=LoopOrigin.ROUTINE))
    await make_loop(init, user, "Dentist appointment")
    await make_loop(init, user, "Send the tax forms")
    await init.executor.apply(user, InitiativeDecision(wakeups=[WakeupRequest(at=later(), reason=reason)]),
                              signal(user, EventType.USER_QUIET, "ev:q", kind="user_quiet"))
    assert await agent_wakeups(init, user) == []


# explicit subjects --------------------------------------------------------------------------------

async def test_explicit_loop_id_binds_and_records_the_subject(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user)
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="nudge on the forms", loop_id=loop.id)]),
        signal(user, EventType.CONNECTION_CHANGED, "ev:c", source="composio"))
    [w] = await agent_wakeups(init, user)
    assert w.loop_id == loop.id and w.payload["subject"] == f"loop:{loop.id}"
    assert w.payload["subject_state"]


@pytest.mark.parametrize("bad", ["closed", "routine", "reasoner", "other_user", "missing"])
async def test_loop_subject_must_be_live_and_the_users(user, clock, recording_bus, fake_memory, bad):
    init = build(recording_bus, fake_memory)
    if bad == "closed":
        loop = await make_loop(init, user, "Return the library books")
        await init.loops.close(loop.id, LoopStatus.DONE)
    elif bad == "routine":
        loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.ROUTINE, title="Morning check-in",
                                                           trust=Trust.SYSTEM, origin=LoopOrigin.ROUTINE))
    elif bad == "reasoner":  # Mavis's own belief or offer is never a subject to chase
        loop = await make_loop(init, user, "Answer to my prep offer", origin=LoopOrigin.REASONER)
    elif bad == "other_user":
        other, _ = await users.get_or_create_by_chat(222, "Someone")
        loop = await init.loops.upsert(other.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Their thing"))
    loop_id = 9999 if bad == "missing" else loop.id
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="look again", loop_id=loop_id)]),
        signal(user, EventType.CONNECTION_CHANGED, f"ev:{bad}", source="composio"))
    assert await agent_wakeups(init, user) == []


@pytest.mark.parametrize("kind", [SubjectKind.APPROVAL, SubjectKind.TASK, SubjectKind.OBSERVATION])
async def test_non_loop_subjects_bind_by_id(user, clock, recording_bus, fake_memory, kind):
    init = build(recording_bus, fake_memory)
    ident = {SubjectKind.APPROVAL: make_approval, SubjectKind.TASK: make_task,
             SubjectKind.OBSERVATION: make_observation}[kind]
    sid = await ident(user)
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="look again", subject_kind=kind.value, subject_id=sid)]),
        signal(user, EventType.CONNECTION_CHANGED, "ev:x", source="composio"))
    [w] = await agent_wakeups(init, user)
    assert w.payload["subject"] == f"{kind.value}:{sid}" and w.loop_id is None


@pytest.mark.parametrize("kind", [SubjectKind.APPROVAL, SubjectKind.TASK])
async def test_finished_non_loop_subjects_are_rejected(user, clock, recording_bus, fake_memory, kind):
    init = build(recording_bus, fake_memory)
    sid = (await make_approval(user, ApprovalStatus.FAILED) if kind is SubjectKind.APPROVAL
           else await make_task(user, TaskStatus.FAILED))
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="look again", subject_kind=kind.value, subject_id=sid)]),
        signal(user, EventType.CONNECTION_CHANGED, "ev:y", source="composio"))
    assert await agent_wakeups(init, user) == []


@pytest.mark.parametrize("etype", [EventType.EVENT_STARTING, EventType.LOOP_CREATED, EventType.EVENT_ENDED])
async def test_the_signals_own_subject_binds_when_the_model_names_none(user, clock, recording_bus,
                                                                       fake_memory, etype):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Car service")
    key = "id" if etype is EventType.LOOP_CREATED else "loop_id"
    await init.executor.apply(user, InitiativeDecision(wakeups=[WakeupRequest(at=later(), reason="again")]),
                              signal(user, etype, f"ev:{etype}", **{key: loop.id}))
    [w] = await agent_wakeups(init, user)
    assert w.loop_id == loop.id  # EVENT_ENDED no longer detaches: closing the loop cancels it


async def test_ended_loop_wakeup_is_cancelled_when_the_loop_closes(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Hand over the keys")
    recording_bus.take()
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(20), reason="did the handover happen", loop_id=loop.id)]),
        signal(user, EventType.EVENT_ENDED, "ev:end", loop_id=loop.id))
    assert len(await agent_wakeups(init, user)) == 1
    await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    for e in recording_bus.take():
        await init.handler.handle(e)
    assert await agent_wakeups(init, user) == []


# no self-continuing chains -----------------------------------------------------------------------

async def _fire(init, user, recording_bus):
    """Fire the one pending AGENT wakeup the way the timer does and return its event."""
    [w] = await agent_wakeups(init, user)
    await init.wakeups.fire_due(w.due_at, lambda _w: _noop())
    return wakeup_event(w)


async def _noop():
    return None


@pytest.mark.parametrize("kind", ["loop", "approval", "task"])
async def test_wakeup_run_cannot_rearm_its_unchanged_subject(user, clock, recording_bus, fake_memory, kind):
    init = build(recording_bus, fake_memory)
    if kind == "loop":
        req = {"loop_id": (await make_loop(init, user, "Renew the car insurance")).id}
    elif kind == "approval":
        req = {"subject_kind": "approval", "subject_id": await make_approval(user)}
    else:
        req = {"subject_kind": "task", "subject_id": await make_task(user)}
    decision = InitiativeDecision(wakeups=[WakeupRequest(at=later(), reason="a", **req)])
    await init.executor.apply(user, decision,
                              signal(user, EventType.CONNECTION_CHANGED, "ev:first", source="composio"))
    clock.set(later(7))
    fired = await _fire(init, user, recording_bus)
    decision = InitiativeDecision(wakeups=[WakeupRequest(at=later(), reason="b", **req)])
    await init.executor.apply(user, decision,
                              fired)
    assert await agent_wakeups(init, user) == []  # same subject, same state: the chain stops here


async def test_wakeup_run_may_rearm_after_the_subject_changed(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Book the venue")
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="a", loop_id=loop.id)]),
        signal(user, EventType.CONNECTION_CHANGED, "ev:first", source="composio"))
    clock.set(later(7))
    fired = await _fire(init, user, recording_bus)
    await init.loops.upsert(user.id, LoopUpsert(id=loop.id, due_at=later(72)))  # the user moved it
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="b", loop_id=loop.id)]), fired)
    assert len(await agent_wakeups(init, user)) == 1


async def test_reasoner_own_track_is_not_a_state_change(user, clock, recording_bus, fake_memory):
    """The run's own edits (applied before its wakeups) do not count as the subject changing."""
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Ship the parcel")
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(), reason="a", loop_id=loop.id)]),
        signal(user, EventType.CONNECTION_CHANGED, "ev:first", source="composio"))
    clock.set(later(7))
    fired = await _fire(init, user, recording_bus)
    await init.executor.apply(user, InitiativeDecision(
        track=[LoopUpsert(id=loop.id, due_at=later(48))],
        wakeups=[WakeupRequest(at=later(), reason="b", loop_id=loop.id)]), fired)
    assert await agent_wakeups(init, user) == []


# fire-time revalidation ---------------------------------------------------------------------------

def _count_reasoner(monkeypatch) -> dict:
    called = {"n": 0}

    async def counting(*a, **k):
        called["n"] += 1
        return InitiativeDecision()

    monkeypatch.setattr(Reasoner, "decide", counting)
    return called


@pytest.mark.parametrize("kind", ["approval", "task", "loop"])
async def test_wakeup_whose_subject_closed_never_reaches_the_reasoner(user, clock, recording_bus, fake_memory,
                                                                     monkeypatch, kind):
    init = build(recording_bus, fake_memory)
    called = _count_reasoner(monkeypatch)
    if kind == "approval":
        sid = await make_approval(user)
        await approvals.set_status(sid, ApprovalStatus.FAILED)
        payload = {"subject": f"approval:{sid}"}
    elif kind == "task":
        sid = await make_task(user)
        await tasks.set_status(sid, TaskStatus.CANCELLED)
        payload = {"subject": f"task:{sid}"}
    else:
        loop = await make_loop(init, user, "Collect the passport")
        await init.loops.close(loop.id, LoopStatus.DONE)
        payload = {"subject": f"loop:{loop.id}", "loop_id": loop.id}
    await init.handler.handle(signal(user, EventType.WAKEUP, "wakeup:500", kind="agent", reason="r",
                                     wakeup_id=500, **payload))
    assert called["n"] == 0


async def test_legacy_unbound_agent_wakeup_is_dropped(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    called = _count_reasoner(monkeypatch)
    await init.handler.handle(signal(user, EventType.WAKEUP, "wakeup:501", kind="agent", reason="check",
                                     wakeup_id=501, loop_id=None))
    assert called["n"] == 0


async def test_live_bound_agent_wakeup_reaches_the_reasoner(user, clock, recording_bus, fake_memory,
                                                            monkeypatch):
    init = build(recording_bus, fake_memory)
    called = _count_reasoner(monkeypatch)
    sid = await make_task(user)
    await init.handler.handle(signal(user, EventType.WAKEUP, "wakeup:502", kind="agent", reason="r",
                                     wakeup_id=502, subject=f"task:{sid}"))
    assert called["n"] == 1


# subject derivation ------------------------------------------------------------------------------

@pytest.mark.parametrize(("etype", "payload", "expected"), [
    (EventType.WAKEUP, {"subject": "approval:4", "loop_id": None}, Subject(SubjectKind.APPROVAL, 4)),
    (EventType.EVENT_ENDED, {"loop_id": 7}, Subject(SubjectKind.LOOP, 7)),
    (EventType.LOOP_CREATED, {"id": 8}, Subject(SubjectKind.LOOP, 8)),
    (EventType.TASK_COMPLETED, {"task_id": 3}, Subject(SubjectKind.TASK, 3)),
    (EventType.USER_QUIET, {"asked_at": "x"}, None),
    (EventType.WAKEUP, {"subject": "nonsense"}, None),
])
def test_event_subject_reads_explicit_ids_only(user, etype, payload, expected):
    ev = Event(id="e", user_id=1, type=etype, occurred_at=timeutil.now(), source="timer", payload=payload)
    assert event_subject(ev) == expected


async def test_resolve_refuses_another_users_rows(user, clock, recording_bus, fake_memory):
    other, _ = await users.get_or_create_by_chat(333, "Other")
    sid = await make_task(other)
    assert await resolve(user.id, Subject(SubjectKind.TASK, sid)) is None
    assert (await resolve(other.id, Subject(SubjectKind.TASK, sid))).live


@pytest.mark.parametrize(("payload", "shown"), [
    ({"subject": "task:12", "kind": "agent"}, "subject_kind task, subject_id 12"),
    ({"subject": "observation:5", "kind": "agent"}, "subject_kind observation, subject_id 5"),
    ({"loop_id": 3, "kind": "agent"}, "subject_kind loop, subject_id 3"),
])
async def test_reasoner_prompt_names_the_signals_subject(user, clock, fake_memory, fake_llm, payload, shown):
    from mavis.initiative.filters import FilterResult
    from mavis.policy.pings import PingPolicy

    fake_llm.push_structured(InitiativeDecision())
    ev = Event(id="wakeup:9", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(),
               source="timer", payload=payload)
    await Reasoner(fake_memory, PingPolicy()).decide(user, ev, FilterResult(drop=False, summary="s"))
    call = fake_llm.structured_calls[-1]
    assert shown in call["user"]
    assert "without a valid subject is discarded" in call["system"]
