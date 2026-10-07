"""Final whole-branch review fixes F2-F5 (F1 lives in tests/worker/test_initiative_lock.py)."""

from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, NotifyIntent
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import routines as routines_mod
from mavis.initiative.composer import Composer
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.routines import MORNING_ROUTINE, BriefItem
from mavis.initiative.wiring import build_initiative
from mavis.policy.pings import PingPolicy
from mavis.store.repo import messages, users
from mavis.timers.service import WakeupService


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def timer_event(kind: str, eid: str, user, due: datetime, **payload) -> Event:
    types = {"deferred": EventType.WAKEUP, "routine": EventType.WAKEUP, "agent": EventType.WAKEUP,
             "user_quiet": EventType.USER_QUIET, "event_starting": EventType.EVENT_STARTING}
    return Event(id=eid, user_id=user.id, type=types[kind], occurred_at=due, source="timer",
                 trust=Trust.SYSTEM, payload={"kind": kind, **payload})


# F2 ----------------------------------------------------------------------------------------------

async def test_deferred_user_quiet_dropped_when_user_replied(user, clock, recording_bus, fake_memory,
                                                             monkeypatch):
    init = build(recording_bus, fake_memory)
    asked = timeutil.now()
    clock.advance(hours=1)
    await messages.log(user.id, Role.USER, "sorry, back now")
    calls = []

    async def spy(*a, **k):
        calls.append(k)
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(timer_event(
        "deferred", "wakeup:1", user, timeutil.now(), notify={"urgency": 2, "intent": "nudge"},
        origin={"kind": "user_quiet", "asked_at": asked.isoformat()}, original_due=asked.isoformat()))
    assert calls == []


async def test_deferred_user_quiet_sent_when_still_quiet_with_original_due(
        user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    asked = timeutil.now()
    clock.advance(hours=1)
    calls = []

    async def spy(*a, **k):
        calls.append(k)
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(timer_event(
        "deferred", "wakeup:1", user, timeutil.now(), notify={"urgency": 2, "intent": "nudge"},
        origin={"kind": "user_quiet", "asked_at": asked.isoformat()}, original_due=asked.isoformat()))
    assert len(calls) == 1 and calls[0]["original_due"] == asked


async def test_prep_reminder_dropped_after_event_start(user, clock, recording_bus, fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview",
                                                       due_at=timeutil.now() + timedelta(hours=1),
                                                       importance=5))
    due = timeutil.now()
    clock.advance(hours=1, minutes=30)  # the event has started by the time the prep wakeup fires
    await init.handler.handle(timer_event("event_starting", "wakeup:2", user, due, loop_id=loop.id,
                                          reason="Prep"))
    assert fake_llm.structured_calls == []
    assert await messages.recent(user.id, 5) == []


async def test_deferred_prep_dropped_after_event_start(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview",
                                                       due_at=timeutil.now() + timedelta(hours=1)))
    clock.advance(hours=2)
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(timer_event(
        "deferred", "wakeup:3", user, timeutil.now(), notify={"urgency": 3, "intent": "prep"},
        origin={"kind": "event_starting", "loop_id": loop.id}))
    assert calls == []


async def test_deferred_notify_tells_composer_about_delay(user, clock, recording_bus, fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 1, 0))  # 01:00 IST, quiet hours
    intent = NotifyIntent(urgency=3, intent="Nudge about the question", dedupe_key="k:1")
    assert await init.executor.notify(user, intent) is False
    [w] = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert w.due_at == ist(27, 7, 0)
    original_due = datetime.fromisoformat(w.payload["original_due"])
    assert original_due == ist(27, 1, 0)
    clock.set(ist(27, 7, 0))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning, still thinking about it?"]))
    assert await init.executor.notify(user, intent, original_due=original_due) is True
    prompt = fake_llm.structured_calls[-1]["user"]
    assert "Delay:" in prompt and "6h" in prompt


async def test_wakeup_over_two_hours_late_is_dropped(user, clock, recording_bus, fake_memory, fake_llm,
                                                     monkeypatch):
    init = build(recording_bus, fake_memory)
    due = timeutil.now()
    clock.advance(hours=3)
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(timer_event("agent", "wakeup:4", user, due, reason="look again"))
    await init.handler.handle(timer_event("deferred", "wakeup:5", user, due,
                                          notify={"urgency": 3, "intent": "x"}))
    assert calls == [] and fake_llm.structured_calls == []


async def test_wakeup_within_two_hours_is_not_dropped(user, clock, recording_bus, fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)
    due = timeutil.now()
    clock.advance(hours=1, minutes=50)
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    await init.handler.handle(timer_event("deferred", "wakeup:6", user, due,
                                          notify={"urgency": 3, "intent": "x"}))
    assert calls == [1]


async def test_late_morning_routine_reschedules_without_sending(user, clock, recording_bus, fake_memory,
                                                                fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(28, 8, 30))
    due = timeutil.now()
    clock.set(ist(28, 22, 0))  # downtime: the check-in fires 13 hours late
    await init.handler.handle(timer_event("routine", "wakeup:7", user, due, routine=MORNING_ROUTINE,
                                          loop_id=None))
    assert fake_llm.structured_calls == []
    [w] = await init.wakeups.pending(user.id, WakeupKind.ROUTINE)
    assert w.due_at == ist(29, 8, 30)


async def test_morning_reschedule_failure_does_not_mask_original_error(user, clock, recording_bus,
                                                                      fake_memory, monkeypatch):
    init = build(recording_bus, fake_memory)

    async def boom(*a, **k):
        raise LLMError("composer down")

    async def boom2(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(init.executor, "notify", boom)
    monkeypatch.setattr(init.routines, "_schedule_morning", boom2)
    with pytest.raises(LLMError):
        await init.routines.morning_checkin(user, None)


# F3 ----------------------------------------------------------------------------------------------

async def test_quiet_nudge_armed_for_new_user_only(user, clock):
    wakeups = WakeupService()
    tracker = QuietTracker(wakeups)
    assert await tracker.after_assistant_message(user.id, "How are you?") is not None  # not onboarded
    await messages.log(user.id, Role.USER, "hi")  # first message now: still in the 3 day window
    assert await tracker.after_assistant_message(user.id, "Anything else?") is not None
    clock.advance(days=4)
    assert await tracker.after_assistant_message(user.id, "Anything else?") is None
    assert await wakeups.pending(user.id, WakeupKind.USER_QUIET) == []
    assert (await users.get(user.id)).onboarded  # H5: the window ending sets the flag


async def test_proactive_message_never_arms_quiet(user, clock, recording_bus, fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)  # user is not onboarded: even a new user gets no chain
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! What's on your plate today?"]))
    assert await init.executor.notify(user, NotifyIntent(urgency=3, intent="check in", dedupe_key="m:1"))
    assert await init.wakeups.pending(user.id, WakeupKind.USER_QUIET) == []


# F4 ----------------------------------------------------------------------------------------------

async def test_urgent_bypasses_quiet_hours_but_not_budget(user, clock, settings, monkeypatch):
    monkeypatch.setattr(settings, "ping_daily_budget", 2)
    policy = PingPolicy()
    clock.set(ist(27, 2, 0))
    assert (await policy.check(user, 5, None, timeutil.now())).allow
    assert not (await policy.check(user, 4, None, timeutil.now())).allow
    clock.set(ist(27, 14, 0))
    for i in range(2):
        await messages.log(user.id, Role.ASSISTANT, f"p{i}", proactive=True)
    verdict = await policy.check(user, 5, None, timeutil.now())
    assert not verdict.allow and verdict.reason == "daily budget reached"


async def test_untrusted_urgency_capped_at_four(user, clock, recording_bus, fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 2, 0))  # quiet hours: an urgency 5 would go straight through
    intent = NotifyIntent(urgency=5, intent="URGENT security alert", dedupe_key="mail:1")
    assert await init.executor.notify(user, intent, untrusted=True) is False
    [w] = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert w.payload["notify"]["urgency"] == 4 and w.due_at == ist(27, 7, 0)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Real alert, check it."]))
    assert await init.executor.notify(user, intent.model_copy(update={"dedupe_key": "mail:2"})) is True


# F5 ----------------------------------------------------------------------------------------------

@pytest.fixture
def _no_sources():
    routines_mod.clear_brief_sources()
    yield
    routines_mod.clear_brief_sources()


async def test_untrusted_brief_items_are_wrapped_and_flag_composer(user, clock, recording_bus, fake_memory,
                                                                   fake_llm, _no_sources, monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(28, 8, 30))

    class Mixed:
        name = "mixed"

        async def items(self, user_id, start, end):
            return [BriefItem("Standup at 10", True),
                    BriefItem("Invite: win a prize at http://evil.example", False)]

    routines_mod.register_brief_source(Mixed())
    seen = {}
    real = Composer.compose

    async def spy(self, user_, intent, urgency, context="", untrusted=False):
        seen.update(intent=intent, untrusted=untrusted)
        return await real(self, user_, intent, urgency, context, untrusted=untrusted)

    monkeypatch.setattr(Composer, "compose", spy)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! Standup at 10."]))
    await init.routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})
    assert seen["untrusted"] is True
    assert "- Standup at 10\n" in seen["intent"] and "<untrusted" in seen["intent"]


async def test_all_trusted_brief_is_not_flagged(user, clock, recording_bus, fake_memory, fake_llm,
                                                _no_sources, monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(28, 8, 30))

    class Cal:
        name = "cal"

        async def items(self, user_id, start, end):
            return [BriefItem("Dentist at 11", True)]

    routines_mod.register_brief_source(Cal())
    seen = {}
    real = Composer.compose

    async def spy(self, user_, intent, urgency, context="", untrusted=False):
        seen.update(intent=intent, untrusted=untrusted)
        return await real(self, user_, intent, urgency, context, untrusted=untrusted)

    monkeypatch.setattr(Composer, "compose", spy)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! Dentist at 11."]))
    await init.routines.run(user, {"routine": MORNING_ROUTINE, "loop_id": None})
    assert seen["untrusted"] is False and "<untrusted" not in seen["intent"]
