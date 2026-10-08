"""D6: one intent from the user yields one proactive artifact.

Replays of the live E2E (test user 3): a 3 minute water reminder produced a wake_me wakeup, a loop from
LEARN (due 4 minutes off, different words) and a reasoner wakeup for that loop (3 messages). One focus
block produced two loops (tool and LEARN, different words, same time) and five wakeups. A failed email
produced a wakeup to 'check whether Gmail is now linked' that nobody asked for.
"""

from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent, WakeupRequest
from mavis.domain.events import Event, EventType, Provenance, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopStatus, LoopUpsert
from mavis.domain.memory import Extraction, LoopDraft
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.wiring import build_initiative
from mavis.loops.service import LoopService, loops_from_extraction
from mavis.timers.service import WakeupService


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def at(minutes: int):
    return timeutil.now() + timedelta(minutes=minutes)


def conv() -> Provenance:
    return Provenance(source_ref="tg:update:1", trust=Trust.USER, conversation=True, anchor_at=timeutil.now())


async def reminder(user, minutes, reason):
    return await WakeupService().wake_me(user.id, at(minutes), f"Reminder the user asked for: {reason}",
                                         kind=WakeupKind.AGENT, reminder=True, scale=False)


async def open_loops(svc, user):
    return [lp for lp in await svc.active(user.id) if lp.status is LoopStatus.OPEN]


# --- loop identity: the same thing said twice, by two writers -------------------------------------------


@pytest.mark.parametrize(("first", "second"), [
    ("Keep Fri 9 Oct 2 to 3 PM blocked for solo focus time", "Focus block scheduled for Fri 9 Oct at 2 PM"),
    ("Remind User to drink water (asked Thu 8 Oct, 3 min later)",
     "Remind User to drink water (set Thu 8 Oct 2026, 19:11 IST)"),
    ("Block 4 PM for deep work", "Deep work block today at 4 PM"),
])
async def test_two_writers_same_moment_same_thing_is_one_loop(user, recording_bus, clock, first, second):
    svc = LoopService(recording_bus)
    due = at(60 * 20)
    a = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=first, due_at=due,
                                             source="tool:track_loop", origin=LoopOrigin.CONVERSATION))
    b = await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=second, due_at=due,
                                             source="tg:update:9", origin=LoopOrigin.CONVERSATION))
    assert a.id == b.id and len(await open_loops(svc, user)) == 1


@pytest.mark.parametrize(("first", "second", "gap"), [
    ("Call Tom about the lease", "Call mom", 0),
    ("Dentist appointment", "Team standup", 0),
    ("Focus block", "Focus block", 60 * 5),  # same words, a different day's slot
    ("Lunch with Raj", "Lunch with Priya", 0),
])
async def test_different_things_at_the_same_time_stay_apart(user, recording_bus, clock, first, second, gap):
    svc = LoopService(recording_bus)
    due = at(60 * 20)
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=first, due_at=due, entities=["Raj"]
                                         if "Raj" in first else []))
    await svc.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=second,
                                         due_at=due + timedelta(minutes=gap), entities=["Priya"]
                                         if "Priya" in second else []))
    assert len(await open_loops(svc, user)) == 2


# --- a reminder the user asked for owns its moment -----------------------------------------------------


@pytest.mark.parametrize(("reason", "title", "offset", "covered"), [
    ("Remind Arjun to drink water", "Remind User to drink water (asked Thu 8 Oct, 3 min later)", -4, True),
    ("Stand up and stretch", "Stretch break", 6, True),
    ("Call the plumber", "Call the plumber about the leak", 0, True),
    ("Call the plumber", "Call the plumber about the leak", 120, False),  # two hours later: another thing
    ("Drink water", "Submit the tax forms", 0, False),  # same moment, unrelated
])
async def test_loop_draft_covered_by_pending_reminder(user, recording_bus, clock, reason, title, offset,
                                                      covered):
    svc = LoopService(recording_bus)
    await reminder(user, 3, reason)
    due = at(3 + offset)
    from mavis.domain.timeutil import to_local

    local_naive = to_local(due, user.timezone).replace(tzinfo=None)
    await loops_from_extraction(svc, user.id, Extraction(loops=[
        LoopDraft(kind="COMMITMENT", title=title, due_at=local_naive)]), conv())
    assert (len(await open_loops(svc, user)) == 0) is covered


async def test_water_reminder_replay_sends_one_reminder_not_three(user, recording_bus, fake_memory, clock):
    """Order of the live run: LEARN's loop (and the reasoner wakeup for it) exist before the wake_me."""
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title="Remind User to drink water", due_at=at(3), importance=2,
        trust=Trust.SYSTEM, origin=LoopOrigin.CONVERSATION))
    nudge = await init.wakeups.wake_me(
        user.id, at(3), f"Water reminder the user asked for: loop {loop.id} is due soon", loop.id,
        WakeupKind.AGENT, dedupe_key=f"agent:loop:{loop.id}:water")
    await reminder(user, 4, "Remind Arjun to drink water")
    pending = {w.id for w in await init.wakeups.pending(user.id, WakeupKind.AGENT)}
    assert nudge not in pending and len(pending) == 1


async def test_unrelated_loop_wakeup_survives_a_new_reminder(user, recording_bus, fake_memory, clock):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title="Submit the tax forms", due_at=at(3), importance=3,
        trust=Trust.SYSTEM, origin=LoopOrigin.CONVERSATION))
    keep = await init.wakeups.wake_me(user.id, at(3), "Tax forms are due: submit them", loop.id,
                                      WakeupKind.AGENT, dedupe_key="agent:tax")
    await reminder(user, 4, "Drink water")
    assert keep in {w.id for w in await init.wakeups.pending(user.id, WakeupKind.AGENT)}


async def test_reasoner_wakeup_for_a_moment_a_reminder_owns_is_not_scheduled(user, recording_bus, fake_memory,
                                                                              clock):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title="Keep 2 to 3 PM blocked for solo focus time", due_at=at(60 * 20),
        importance=3, trust=Trust.SYSTEM, origin=LoopOrigin.CONVERSATION))
    await reminder(user, 60 * 20, "Focus block starting: 2 to 3 PM solo focus")
    event = Event(id="loop:x:created", user_id=user.id, type=EventType.LOOP_CREATED,
                  occurred_at=timeutil.now(), source="agent", payload=loop.model_dump(mode="json"),
                  trust=Trust.SYSTEM)
    decision = InitiativeDecision(wakeups=[WakeupRequest(
        at=at(60 * 20 - 5), reason="Focus block starts in 5 minutes", loop_id=loop.id)])
    await init.executor.apply(user, decision, event)
    mine = [w for w in await init.wakeups.pending(user.id, WakeupKind.AGENT) if w.loop_id == loop.id]
    assert mine == []


# --- wake_me idempotency ---------------------------------------------------------------------------


@pytest.mark.parametrize(("a", "b", "same"), [
    ("Drink water", "Remind Arjun to drink water", True),
    ("Call Raj back", "Call Raj", True),
    ("Drink water", "Take the medicine", False),
])
async def test_same_reminder_asked_twice_in_different_words_is_one_wakeup(user, clock, a, b, same):
    first = await reminder(user, 3, a)
    second = await reminder(user, 3, b)
    assert (first == second) is same


async def test_same_reminder_at_a_different_time_is_a_second_wakeup(user, clock):
    assert await reminder(user, 3, "Drink water") != await reminder(user, 90, "Drink water")


# --- no pings about connecting services nobody asked about ---------------------------------------------


@pytest.mark.parametrize("reason", [
    "Check whether Gmail is now linked so the short 'hi' email to test@example.com can be sent",
    "See if the user has connected Notion yet",
    "Remind the user to link their Google Calendar",
    "Follow up on authorising Slack access",
])
async def test_reasoner_wakeup_about_connecting_a_service_is_dropped(user, recording_bus, fake_memory, clock,
                                                                      reason):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title="Send an email to test@example.com saying hi", importance=2,
        trust=Trust.SYSTEM, origin=LoopOrigin.CONVERSATION))
    event = Event(id=f"loop:{loop.id}:created", user_id=user.id, type=EventType.LOOP_CREATED,
                  occurred_at=timeutil.now(), source="agent", payload=loop.model_dump(mode="json"),
                  trust=Trust.SYSTEM)
    await init.executor.apply(user, InitiativeDecision(
        wakeups=[WakeupRequest(at=at(60 * 12), reason=reason, loop_id=loop.id)]), event)
    assert await init.wakeups.pending(user.id, WakeupKind.AGENT) == []


@pytest.mark.parametrize("wakeup_reason", [
    "Look at the quarterly report once the numbers land",
    "Check whether the landlord answered",
    "Gmail digest: three threads are waiting on you",  # names a service but is not about connecting it
])
async def test_ordinary_wakeups_are_still_scheduled(user, recording_bus, fake_memory, clock, wakeup_reason):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    loop = await init.loops.upsert(user.id, LoopUpsert(
        kind=LoopKind.COMMITMENT, title="Review the quarterly report", importance=3, trust=Trust.SYSTEM,
        origin=LoopOrigin.CONVERSATION))
    event = Event(id=f"loop:{loop.id}:created", user_id=user.id, type=EventType.LOOP_CREATED,
                  occurred_at=timeutil.now(), source="agent", payload=loop.model_dump(mode="json"),
                  trust=Trust.SYSTEM)
    await init.executor.apply(user, InitiativeDecision(
        wakeups=[WakeupRequest(at=at(60 * 12), reason=wakeup_reason, loop_id=loop.id)]), event)
    assert len(await init.wakeups.pending(user.id, WakeupKind.AGENT)) == 1


async def test_reasoner_notify_about_connecting_a_service_is_not_sent(user, recording_bus, fake_memory, clock,
                                                                       sent):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    event = Event(id="wakeup:5", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(),
                  source="timer", payload={"kind": "agent"}, trust=Trust.SYSTEM)
    await init.executor.apply(user, InitiativeDecision(notify=NotifyIntent(
        urgency=3, intent="Tell the user Gmail still isn't linked and ask them to connect it",
        dedupe_key="gmail-link")), event)
    assert sent == []
