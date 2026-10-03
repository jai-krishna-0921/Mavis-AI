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
                                        payload={"kind": "agent", "reason": "thanks", "wakeup_id": 70 + i}))
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


# F1 ----------------------------------------------------------------------------------------------

def agent_event(user, loop_id, eid: str = "wakeup:60") -> Event:
    return Event(id=eid, user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer",
                 trust=Trust.SYSTEM, payload={"kind": "agent", "loop_id": loop_id, "wakeup_id": 60,
                                              "reason": "Second reminder before flight"})


async def test_deferred_reminder_is_dropped_once_the_event_has_passed(user, clock, recording_bus, fake_memory,
                                                                      fake_llm, monkeypatch):
    from mavis.domain.wakeups import WakeupKind
    from mavis.timers.runner import wakeup_event

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 23, 30))  # quiet hours
    flight = ist(28, 0, 30)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Flight to Delhi",
                                                       due_at=flight, importance=5))
    recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent="prep for the flight")))
    await init.handler.handle(agent_event(user, loop.id))
    [deferred] = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert deferred.loop_id == loop.id
    assert deferred.payload["valid_until"] == flight.isoformat()

    calls = spy_notify(init, monkeypatch)
    clock.set(deferred.due_at)  # 07:00, the flight left at 00:30
    await init.handler.handle(wakeup_event(deferred))
    assert calls == []


async def test_deferred_ping_dropped_when_its_loop_closed(user, clock, recording_bus, fake_memory,
                                                          monkeypatch):
    from mavis.domain.loops import LoopStatus

    init = build(recording_bus, fake_memory)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from Jawahar"))
    await init.loops.close(loop.id, LoopStatus.DONE)
    calls = spy_notify(init, monkeypatch)
    await init.handler.handle(Event(
        id="wakeup:61", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer",
        payload={"kind": "deferred", "wakeup_id": 61, "loop_id": loop.id,
                 "notify": {"urgency": 3, "intent": "any word from Jawahar?"},
                 "origin": {"kind": "wakeup", "loop_id": loop.id}}))
    assert calls == []


async def test_old_deferred_payload_without_valid_until_expires(user, clock, recording_bus, fake_memory,
                                                                monkeypatch):
    init = build(recording_bus, fake_memory)
    calls = spy_notify(init, monkeypatch)
    base = {"kind": "deferred", "wakeup_id": 62, "notify": {"urgency": 3, "intent": "weekly summary"}}
    stale = (timeutil.now() - timedelta(hours=13)).isoformat()
    fresh = (timeutil.now() - timedelta(hours=9)).isoformat()
    for i, due in enumerate([stale, fresh]):
        await init.handler.handle(Event(id=f"wakeup:{62 + i}", user_id=user.id, type=EventType.WAKEUP,
                                        occurred_at=timeutil.now(), source="timer",
                                        payload={**base, "original_due": due}))
    assert len(calls) == 1


# F3 ----------------------------------------------------------------------------------------------

async def test_defaults_always_scheduled_and_near_llm_wakeup_merges(user, clock, recording_bus, fake_memory,
                                                                    fake_llm):
    from mavis.domain.decisions import WakeupRequest
    from mavis.domain.wakeups import WakeupKind

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    due = ist(28, 10, 0)
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT,
                                                       title="Interview with Jawahar", due_at=due,
                                                       importance=5))
    [created] = recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(wakeups=[
        WakeupRequest(at=due - timedelta(minutes=50), reason="Final prep before the interview with Jawahar"),
        WakeupRequest(at=due - timedelta(hours=14), reason="Evening review of interview notes",
                      loop_id=loop.id),
    ]))
    await init.handler.handle(created)
    pending = await init.wakeups.pending(user.id)
    kinds = sorted((w.kind.value, w.due_at) for w in pending)
    assert kinds == sorted([
        (WakeupKind.AGENT.value, due - timedelta(hours=14)),
        (WakeupKind.EVENT_STARTING.value, due - timedelta(hours=1)),  # absorbed the 50-min LLM wakeup
        (WakeupKind.EVENT_ENDED.value, due + timedelta(hours=2)),
    ])
    assert all(w.loop_id == loop.id for w in pending)


async def test_llm_wakeup_gets_loop_id_and_closing_cancels_it(user, clock, recording_bus, fake_memory,
                                                              fake_llm):
    from mavis.domain.decisions import WakeupRequest
    from mavis.domain.loops import LoopStatus

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from Jawahar"))
    [created] = recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(wakeups=[
        WakeupRequest(at=ist(29, 10, 0), reason="Nudge if still nothing", loop_id=999),  # invented id
    ]))
    await init.handler.handle(created)
    [w] = await init.wakeups.pending(user.id)
    assert w.loop_id == loop.id  # the event's loop, not the id the model made up
    await init.loops.close(loop.id, LoopStatus.DONE)
    [updated] = recording_bus.take()
    await init.handler.handle(updated)
    assert await init.wakeups.pending(user.id) == []


async def test_wakeup_reason_naming_another_loop_attaches_to_it(user, clock, recording_bus, fake_memory,
                                                                fake_llm):
    from mavis.domain.decisions import WakeupRequest

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    interview = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT,
                                                            title="Fractal interview", due_at=ist(30, 10, 0),
                                                            importance=2))
    concern = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.CONCERN, title="Nervous about money"))
    recording_bus.take()
    created = Event(id=f"loop:{concern.id}:created", user_id=user.id, type=EventType.LOOP_CREATED,
                    occurred_at=timeutil.now(), source="agent", payload=concern.model_dump(mode="json"))
    fake_llm.push_structured(InitiativeDecision(wakeups=[
        WakeupRequest(at=ist(29, 18, 0), reason="Check prep for the Fractal interview"),
    ]))
    await init.handler.handle(created)
    agent = [w for w in await init.wakeups.pending(user.id) if w.kind.value == "agent"]
    assert [w.loop_id for w in agent] == [interview.id]


# F4 ----------------------------------------------------------------------------------------------

async def test_wakeup_from_untrusted_event_fires_untrusted(user, clock, recording_bus, fake_memory, fake_llm):
    from mavis.domain.decisions import ComposedMessage, WakeupRequest
    from mavis.store.repo import outbox
    from mavis.timers.runner import wakeup_event

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    email = Event(id="gmail:msg:x1", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                  occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                  payload={"from": "billing@vendor.example", "subject": "Invoice", "snippet": "pay soon"})
    fake_llm.push_structured(InitiativeDecision(wakeups=[
        WakeupRequest(at=ist(27, 18, 0), reason="Pay at http://evil.example now")]))
    await init.handler.handle(email)
    [w] = await init.wakeups.pending(user.id)
    event = wakeup_event(w)
    assert event.trust is Trust.UNTRUSTED

    clock.set(ist(27, 18, 0))
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=5, intent="pay the invoice",
                                                                    dedupe_key="invoice")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Pay it at http://evil.example/pay"]))
    await init.handler.handle(event)
    reasoner_prompt = fake_llm.structured_calls[1]["user"]
    assert "<untrusted" in reasoner_prompt
    assert await outbox.texts_with_dedupe_prefix("invoice:") == ["Pay it at (check it directly)"]


# F5 ----------------------------------------------------------------------------------------------

async def test_retry_reuses_the_persisted_decision(user, clock, recording_bus, fake_memory, fake_llm,
                                                   monkeypatch):
    import pytest

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    email = Event(id="gmail:msg:sec", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                  occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                  payload={"from": "no-reply@accounts.example", "subject": "Security alert", "snippet": "x"})
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent="was that you?",
                                                                    dedupe_key="sec")))
    real_apply = init.executor.apply
    attempts = {"n": 0}

    async def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("db blip after side effects")
        return await real_apply(*a, **k)

    monkeypatch.setattr(init.executor, "apply", flaky)
    calls = spy_notify(init, monkeypatch)
    with pytest.raises(RuntimeError):
        await init.handler.handle(email)
    await init.handler.handle(email)  # the retry: the fake LLM has nothing left, so a re-ask would fail
    assert len(fake_llm.structured_calls) == 1
    assert [c["intent"].intent for c in calls] == ["was that you?"]
