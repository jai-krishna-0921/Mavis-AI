"""H5 review fix round 1: only external changes reopen a chain, AWAITING expires, quiet wiring, slot order."""

from datetime import timedelta

import pytest

from mavis.attention.schema import AttentionDecision, Verdict
from mavis.attention.speaker import Speaker
from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, NotifyIntent, WakeupRequest
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.messages import Outbound, Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.wiring import build_initiative
from mavis.policy.pings import PingPolicy, subject_ping_key
from mavis.store.db import Session
from mavis.store.repo import attention, messages, outbox
from mavis.store.repo import loops as loops_repo
from mavis.timers.runner import wakeup_event
from mavis.timers.service import WakeupService


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def later(hours: float):
    return timeutil.now() + timedelta(hours=hours)


def timer(user, etype: EventType, eid: str, **payload) -> Event:
    return Event(id=eid, user_id=user.id, type=etype, occurred_at=timeutil.now(), source="timer",
                 trust=Trust.SYSTEM, payload={"wakeup_id": 1, "reason": "r", **payload})


async def agent_wakeups(init, user):
    return await init.wakeups.pending(user.id, WakeupKind.AGENT)


async def make_loop(init, user, title, kind=LoopKind.COMMITMENT, hours=30, origin=LoopOrigin.CONVERSATION):
    return await init.loops.upsert(user.id, LoopUpsert(kind=kind, title=title, due_at=later(hours),
                                                       importance=3, trust=Trust.SYSTEM, origin=origin))


async def fire_one(init, user):
    [w] = await agent_wakeups(init, user)

    async def noop(_w):
        return None

    await init.wakeups.fire_due(w.due_at, noop)
    return wakeup_event(w)


# I1: the reasoner's own edits never count as the subject changing, across runs and days -------------

@pytest.mark.parametrize(("title", "kind"), [
    ("Renew the car insurance", LoopKind.COMMITMENT),
    ("Reply from the landlord", LoopKind.WAITING_ON),
    ("Run a 10k before December", LoopKind.GOAL),
])
async def test_multi_day_replan_chain_stops(user, clock, recording_bus, fake_memory, title, kind):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, title, kind)
    due = loop.due_at
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(6), reason="look again", loop_id=loop.id)]),
        Event(id="ev:first", user_id=user.id, type=EventType.CONNECTION_CHANGED, occurred_at=timeutil.now(),
              source="composio"))
    clock.set(later(7))
    triggers = [await fire_one(init, user),
                timer(user, EventType.EVENT_STARTING, "wakeup:s1", kind="event_starting", loop_id=loop.id),
                timer(user, EventType.EVENT_ENDED, "wakeup:e1", kind="event_ended", loop_id=loop.id)]
    for day, trigger in enumerate(triggers, start=1):
        recording_bus.take()
        bump = InitiativeDecision(track=[LoopUpsert(id=loop.id, due_at=due + timedelta(days=day))],
                                  wakeups=[WakeupRequest(at=later(20), reason=f"day {day}", loop_id=loop.id)])
        await init.executor.apply(user, bump, trigger)
        assert (await init.loops.get(loop.id)).due_at == due  # the reasoner's due move is only a note
        assert not [e for e in recording_bus.take() if e.type is EventType.LOOP_UPDATED]  # no re-plan
        assert await agent_wakeups(init, user) == []  # the chain stays stopped
        clock.advance(days=1)


async def test_external_change_reopens_one_recheck(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Book the venue")
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(6), reason="a", loop_id=loop.id)]),
        Event(id="ev:first", user_id=user.id, type=EventType.CONNECTION_CHANGED, occurred_at=timeutil.now(),
              source="composio"))
    clock.set(later(7))
    fired = await fire_one(init, user)
    # the user moves it in chat (a conversation-origin write): that is an external change
    await init.loops.upsert(user.id, LoopUpsert(id=loop.id, due_at=later(72), origin=LoopOrigin.CONVERSATION))
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(20), reason="b", loop_id=loop.id)]), fired)
    assert len(await agent_wakeups(init, user)) == 1


async def test_mavis_own_follow_up_is_not_a_state_change(user, clock, recording_bus, fake_memory):
    """OPEN to AWAITING happens because Mavis asked: it does not reopen the subject for re-checks."""
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Dentist check-up")
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(6), reason="a", loop_id=loop.id)]),
        Event(id="ev:first", user_id=user.id, type=EventType.CONNECTION_CHANGED, occurred_at=timeutil.now(),
              source="composio"))
    clock.set(later(7))
    fired = await fire_one(init, user)
    await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(20), reason="b", loop_id=loop.id)]), fired)
    assert await agent_wakeups(init, user) == []


@pytest.mark.parametrize("status", [LoopStatus.AWAITING_REPLY, LoopStatus.OPEN])
async def test_reasoner_cannot_move_status_without_evidence(user, clock, recording_bus, fake_memory, status):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Pay the electricity bill")
    if status is LoopStatus.OPEN:
        await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    before = (await init.loops.get(loop.id)).status
    await init.executor.apply(user, InitiativeDecision(track=[LoopUpsert(id=loop.id, status=status)]),
                              timer(user, EventType.WAKEUP, "wakeup:x", kind="agent", loop_id=loop.id))
    assert (await init.loops.get(loop.id)).status is before


async def test_reasoner_origin_loop_is_not_replanned_on_update(user, clock, recording_bus, fake_memory):
    """M1: consistent with creation, a reasoner belief never gets derived prep/follow-up signals."""
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Missed the workshop", origin=LoopOrigin.REASONER)
    recording_bus.take()
    await init.loops.upsert(user.id, LoopUpsert(id=loop.id, due_at=later(50), origin=LoopOrigin.CONVERSATION))
    for e in recording_bus.take():
        await init.handler.handle(e)
    assert await init.wakeups.pending(user.id) == []


# M4: a timer run's first wakeup is not blocked by one that is merely pending --------------------------

async def test_pending_wakeup_does_not_block_a_timer_runs_first_wakeup(user, clock, recording_bus,
                                                                       fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Interview at Fractal", hours=60)
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(40), reason="prep check", loop_id=loop.id)]),
        Event(id="ev:created", user_id=user.id, type=EventType.LOOP_CREATED, occurred_at=timeutil.now(),
              source="agent", payload={"id": loop.id}))
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(10), reason="early nudge", loop_id=loop.id)]),
        timer(user, EventType.EVENT_STARTING, "wakeup:s", kind="event_starting", loop_id=loop.id))
    assert len(await agent_wakeups(init, user)) == 2


async def test_the_triggering_wakeup_itself_counts_before_it_is_marked_fired(user, clock, recording_bus,
                                                                            fake_memory):
    """An in-process bus may run the handler before fire_due marks the row fired."""
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Return the rental car")
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(6), reason="a", loop_id=loop.id)]),
        Event(id="ev:first", user_id=user.id, type=EventType.CONNECTION_CHANGED, occurred_at=timeutil.now(),
              source="composio"))
    [w] = await agent_wakeups(init, user)
    await init.wakeups.cancel(w.id)  # not fired, not pending: only the event itself carries its state
    await init.executor.apply(user, InitiativeDecision(wakeups=[
        WakeupRequest(at=later(20), reason="b", loop_id=loop.id)]), wakeup_event(w))
    assert await agent_wakeups(init, user) == []


# I3: an unanswered follow-up expires, it is never done --------------------------------------------

async def test_unanswered_follow_up_expires_not_done(user, clock, recording_bus, fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Parent-teacher meeting", hours=-3)
    await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    clock.advance(hours=25)
    assert await init.loops.expire_stale() == 1
    assert (await init.loops.get(loop.id)).status is LoopStatus.EXPIRED
    assert await loops_repo.list_done_since(user.id, timeutil.now() - timedelta(days=2)) == []


# I4: no unreachable subject-bound nudge: after onboarding nothing arms USER_QUIET ------------------

async def test_follow_up_question_arms_no_nudge(user, clock, recording_bus, fake_memory, fake_llm):
    """QA F3 stands: a proactive "how did it go?" never arms a went-quiet nudge, even though its item
    now waits on the answer (AWAITING expires after a day instead)."""
    init = build(recording_bus, fake_memory)
    await messages.log(user.id, Role.USER, "hello")
    clock.advance(days=5)
    loop = await make_loop(init, user, "Visa interview", hours=-2)
    recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ask how it went")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["How did the visa interview go?"]))
    await init.handler.handle(timer(user, EventType.EVENT_ENDED, "wakeup:e", kind="event_ended",
                                    loop_id=loop.id))
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY
    assert await init.wakeups.pending(user.id, WakeupKind.USER_QUIET) == []


@pytest.mark.parametrize("question", ["Want me to draft a note?", "Should I book a cab?",
                                      "Which day suits you for the visa interview?"])
async def test_no_chat_question_arms_a_nudge_after_onboarding(user, clock, recording_bus, fake_memory,
                                                              question):
    init = build(recording_bus, fake_memory)
    await messages.log(user.id, Role.USER, "hello")
    clock.advance(days=5)
    await make_loop(init, user, "Visa interview")
    assert await init.quiet.after_assistant_message(user.id, question) is None


# M2: a crash between reserve and record still finishes the follow-up -------------------------------

async def test_crash_after_enqueue_is_recovered_despite_the_reserved_slot(user, clock, recording_bus,
                                                                          fake_memory):
    init = build(recording_bus, fake_memory)
    loop = await make_loop(init, user, "Hand back the keys", hours=-2)
    origin = {"kind": "event_ended", "loop_id": loop.id, "subject": f"loop:{loop.id}"}
    policy = PingPolicy()
    await policy.reserve(user, subject_ping_key(origin["subject"], "event_ended"), timeutil.now())
    day = timeutil.to_local(timeutil.now(), user.timezone).date().isoformat()
    async with Session() as s:  # the bubble went out, then the worker died before record/log
        await outbox.enqueue(s, Outbound(user_id=user.id, text="How did the handover go?", proactive=True,
                                         dedupe_key=f"keys:{day}:0"))
        await s.commit()
    sent = await init.executor.notify(user, NotifyIntent(urgency=3, intent="ask", dedupe_key="keys"),
                                      origin=origin)
    assert sent is False  # nothing new composed (fake_llm is empty)
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY
    proactive = [m.content for m in await messages.recent(user.id, 5) if m.proactive]
    assert proactive == ["How did the handover go?"]


# M5: the speaker's fixed-text fallback holds the observation's slot --------------------------------

async def test_speaker_fallback_keeps_the_observations_daily_slot(user, clock, recording_bus, fake_memory,
                                                                  fake_llm):
    init = build(recording_bus, fake_memory)
    obs, _ = await attention.insert_pending(user.id, "m-bill", thread_id="", origin="live",
                                            sender_domain="power.example", sender_name="City Power",
                                            received_at=timeutil.now(), payload={})
    await attention.finish(obs.id, kind="deadline_or_bill", verdict="notify", urgency=3,
                           summary="bill due Friday", facts={"codes": []})
    obs = await attention.get(obs.id)
    fake_llm.push_error(LLMError("busy"), structured=True)
    speaker = Speaker(lambda: init.executor, PingPolicy(), WakeupService())
    assert await speaker.speak(user, obs, AttentionDecision(Verdict.NOTIFY, 3, 0.8, ("due",))) == "sent"
    # another trigger about the same observation, the same day, under another key: skipped
    origin = {"kind": "wakeup", "subject": f"observation:{obs.id}"}
    again = await init.executor.notify(user, NotifyIntent(urgency=3, intent="bill", dedupe_key="other"),
                                       untrusted=True, origin=origin)
    assert again is False

