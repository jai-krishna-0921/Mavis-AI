"""Live-QA hardening of the initiative engine (findings F1-F13 and the follow-up close)."""

from datetime import UTC, datetime, timedelta

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopUpsert
from mavis.initiative.wiring import build_initiative


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def build(bus, memory):
    return build_initiative(bus, memory, embed=no_embed)


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def spy_notify(init, monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def spy(user_, intent, context="", quiet_streak=0, untrusted=False, original_due=None, origin=None):
        calls.append({"intent": intent, "untrusted": untrusted, "origin": origin})
        return True

    monkeypatch.setattr(init.executor, "notify", spy)
    return calls


def starting_event(user, loop_id: int, eid: str = "wakeup:50") -> Event:
    return Event(id=eid, user_id=user.id, type=EventType.EVENT_STARTING, occurred_at=timeutil.now(),
                 source="timer", trust=Trust.SYSTEM,
                 payload={"kind": "event_starting", "loop_id": loop_id, "wakeup_id": 50, "reason": "prep"})


# F13 ---------------------------------------------------------------------------------------------

async def test_llm_urgency_capped_at_four_for_pre_event_nudge(user, clock, recording_bus, fake_memory,
                                                              fake_llm, monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview",
                                                       due_at=ist(27, 10, 0), importance=5))
    recording_bus.take()
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=5, intent="prep now")))
    await init.handler.handle(starting_event(user, loop.id))
    assert calls[0]["intent"].urgency == 4


async def test_imminent_event_is_urgent_by_rule(user, clock, recording_bus, fake_memory, fake_llm,
                                                monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 50))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview",
                                                       due_at=ist(27, 10, 0), importance=5))
    recording_bus.take()
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="prep now")))
    await init.handler.handle(starting_event(user, loop.id))
    assert calls[0]["intent"].urgency == 5


# F10 ---------------------------------------------------------------------------------------------

def test_normalize_dedupe_key():
    from mavis.policy.pings import normalize_dedupe_key

    assert normalize_dedupe_key("thank_you_jawahar") == normalize_dedupe_key("Thankyou-Jawahar") \
        == "thankyoujawahar"
    assert normalize_dedupe_key("followup_interview_2026-10-03") == "followupinterview"
    assert normalize_dedupe_key("--") is None and normalize_dedupe_key(None) is None


async def test_llm_key_variants_deliver_once(user, clock, recording_bus, fake_memory, fake_llm, monkeypatch):
    from mavis.domain.decisions import ComposedMessage
    from mavis.store.repo import outbox

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    for i, key in enumerate(["thank_you_jawahar", "thankyou_jawahar"]):
        fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="thank-you email",
                                                                        dedupe_key=key)))
        fake_llm.push_structured(ComposedMessage(send=True, messages=[f"Send that thank-you note {i}"]))
        await init.handler.handle(Event(id=f"wakeup:{70 + i}", user_id=user.id, type=EventType.WAKEUP,
                                        occurred_at=timeutil.now(), source="timer", trust=Trust.SYSTEM,
                                        payload={"kind": "agent", "reason": "thank-you", "wakeup_id": 70 + i}))
    assert await outbox.texts_with_dedupe_prefix("thankyoujawahar:") == ["Send that thank-you note 0"]


async def test_same_loop_and_kind_pings_once_per_day(user, clock, recording_bus, fake_memory, fake_llm):
    from mavis.domain.decisions import ComposedMessage
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview",
                                                       due_at=ist(27, 11, 0), importance=5))
    recording_bus.take()
    for i, key in enumerate(["prep_a", "prep_b"]):
        fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="prep",
                                                                        dedupe_key=key)))
        fake_llm.push_structured(ComposedMessage(send=True, messages=[f"Prep time {i}"]))
        await init.handler.handle(starting_event(user, loop.id, eid=f"wakeup:{80 + i}"))
    sent = [m.content for m in await messages.recent(user.id) if m.proactive]
    assert sent == ["Prep time 0"]
