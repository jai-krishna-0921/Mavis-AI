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
