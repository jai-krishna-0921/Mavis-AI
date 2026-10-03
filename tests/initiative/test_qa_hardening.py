"""Live-QA hardening of the initiative engine (findings F1-F13 and the follow-up close)."""

from datetime import UTC, datetime, timedelta

from mavis.domain import timeutil
from mavis.domain.decisions import InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopKind, LoopOrigin, LoopUpsert
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
    clock.set(ist(27, 7, 30))  # well before the prep window: no deterministic floor applies
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
    clock.set(ist(27, 9, 0))  # the default prep wakeup fires 60 min ahead
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


# F2 ----------------------------------------------------------------------------------------------

async def _chat_loop(init, user, recording_bus):
    data = LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview with Jawahar", due_at=ist(28, 10, 0),
                      importance=5, source="tg:update:5", trust=Trust.USER, origin=LoopOrigin.CONVERSATION)
    loop = await init.loops.upsert(user.id, data)
    [created] = recording_bus.take()
    return loop, created


async def test_loop_from_just_answered_turn_does_not_ping(user, clock, recording_bus, fake_memory, fake_llm,
                                                          monkeypatch):
    from mavis.domain.messages import Role
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 20, 0))
    await messages.log(user.id, Role.USER, "interview with Jawahar tomorrow at 10, nervous")
    await messages.log(user.id, Role.ASSISTANT, "You've got this. Want to run through it tonight?")
    clock.advance(minutes=2)
    loop, created = await _chat_loop(init, user, recording_bus)
    calls = spy_notify(init, monkeypatch)
    offer = NotifyIntent(urgency=4, intent="Offer a mock interview")
    fake_llm.push_structured(InitiativeDecision(notify=offer))
    await init.handler.handle(created)
    assert calls == []
    kinds = {w.kind for w in await init.wakeups.pending(user.id)}
    assert {WakeupKind.EVENT_STARTING, WakeupKind.EVENT_ENDED} <= kinds  # still tracked and scheduled


async def test_loop_from_chat_never_pings_at_creation_even_late(user, clock, recording_bus, fake_memory,
                                                                 fake_llm, monkeypatch):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 20, 0))
    await messages.log(user.id, Role.USER, "interview with Jawahar tomorrow at 10")
    clock.advance(minutes=30)  # e.g. the LEARN job ran late under LLM load
    loop, created = await _chat_loop(init, user, recording_bus)
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="prep reminder")))
    await init.handler.handle(created)
    assert calls == []


async def test_suppression_is_persisted_so_a_late_retry_stays_quiet(user, clock, recording_bus, fake_memory,
                                                                     fake_llm, monkeypatch):
    from mavis.store.repo import decisions

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 20, 0))
    loop, created = await _chat_loop(init, user, recording_bus)
    calls = spy_notify(init, monkeypatch)
    offer = NotifyIntent(urgency=4, intent="Offer a mock interview")
    fake_llm.push_structured(InitiativeDecision(notify=offer))
    await init.handler.handle(created)
    assert (await decisions.get(created.id)).notify is None
    clock.advance(minutes=15)
    await init.handler.handle(created)  # redelivery: reuses the stored, already-quiet decision
    assert calls == []


async def test_loop_from_non_chat_source_may_ping(user, clock, recording_bus, fake_memory, fake_llm,
                                                  monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 20, 0))
    await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Board review",
                                                due_at=ist(28, 10, 0), importance=5, source="gmail:msg:1",
                                                origin=LoopOrigin.REASONER))
    [created] = recording_bus.take()
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="prep reminder")))
    await init.handler.handle(created)
    assert len(calls) == 1


async def test_prompts_forbid_invented_offers(user, clock, fake_memory, fake_llm):
    from mavis.domain.decisions import ComposedMessage
    from mavis.initiative.composer import Composer
    from mavis.initiative.filters import FilterResult
    from mavis.initiative.reasoner import Reasoner
    from mavis.policy.pings import PingPolicy

    fake_llm.push_structured(InitiativeDecision())
    event = Event(id="x", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer")
    await Reasoner(fake_memory, PingPolicy()).decide(user, event, FilterResult(drop=False, summary="s"))
    fake_llm.push_structured(ComposedMessage(send=False))
    await Composer(fake_memory).compose(user, "prep", 3)
    reasoner_system, composer_system = (c["system"] for c in fake_llm.structured_calls)
    assert "Never invent people, companies" in reasoner_system and "mock interview" in reasoner_system
    assert "set its loop_id" in reasoner_system
    assert "Never" in composer_system and "invent people, companies, offers" in composer_system


# follow-up closes on the user's reply, not when it is sent -----------------------------------------

async def _awaiting_loop(init, user, recording_bus, title="Interview with Jawahar"):
    from mavis.domain.loops import LoopStatus

    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title=title,
                                                       due_at=ist(27, 10, 0), importance=5))
    await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    recording_bus.take()
    return loop


async def test_reply_naming_the_loop_closes_it(user, clock, recording_bus, fake_memory):
    from mavis.domain.loops import LoopStatus

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    loop = await _awaiting_loop(init, user, recording_bus)
    assert await init.loops.on_user_message(user.id, "what's for dinner?") == 0
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY
    assert await init.loops.on_user_message(user.id, "the interview was fine, Jawahar was kind") == 1
    assert (await init.loops.get(loop.id)).status is LoopStatus.DONE


async def test_first_reply_to_the_follow_up_closes_it(user, clock, recording_bus, fake_memory):
    from mavis.domain.loops import LoopStatus
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    await messages.log(user.id, Role.ASSISTANT, "How did it go?", proactive=True)  # the follow-up
    loop = await _awaiting_loop(init, user, recording_bus)  # marked right after delivery
    clock.advance(minutes=20)
    await messages.log(user.id, Role.USER, "pretty good actually")
    assert await init.loops.on_user_message(user.id, "pretty good actually") == 1
    assert (await init.loops.get(loop.id)).status is LoopStatus.DONE


async def test_unanswered_follow_up_closes_after_a_day(user, clock, recording_bus, fake_memory):
    from mavis.domain.loops import LoopStatus

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    loop = await _awaiting_loop(init, user, recording_bus)
    clock.advance(hours=23)
    assert await init.loops.expire_stale() == 0
    clock.advance(hours=2)
    assert await init.loops.expire_stale() == 1
    assert (await init.loops.get(loop.id)).status is LoopStatus.DONE


async def test_awaiting_keeps_a_deferred_follow_up(user, clock, recording_bus, fake_memory):
    from mavis.domain.wakeups import WakeupKind

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Dentist",
                                                       due_at=ist(27, 10, 0), importance=3))
    recording_bus.take()
    await init.wakeups.wake_me(user.id, ist(28, 7, 0), "deferred: how did it go", loop.id,
                               WakeupKind.DEFERRED, scale=False)
    from mavis.domain.loops import LoopStatus

    await init.loops.close(loop.id, LoopStatus.AWAITING_REPLY)
    for event in recording_bus.take():
        await init.handler.handle(event)
    assert [w.kind for w in await init.wakeups.pending(user.id)] == [WakeupKind.DEFERRED]


# review I6 ---------------------------------------------------------------------------------------

async def test_missing_decisions_table_fails_open(user, clock, recording_bus, fake_memory, fake_llm,
                                                  monkeypatch):
    from sqlalchemy import text

    from mavis.store.db import Session

    async with Session() as s:
        await s.execute(text("DROP TABLE initiative_decisions"))
        await s.commit()
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="hello")))
    await init.handler.handle(Event(id="wakeup:90", user_id=user.id, type=EventType.WAKEUP,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"kind": "agent", "reason": "check", "wakeup_id": 90}))
    assert len(calls) == 1


# review I4 ---------------------------------------------------------------------------------------

async def test_security_notice_survives_the_budget(user, clock, recording_bus, fake_memory, fake_llm,
                                                   settings, monkeypatch):
    from mavis.domain.decisions import ComposedMessage
    from mavis.domain.messages import Role
    from mavis.initiative import hooks
    from mavis.initiative.email_triage import EmailTriage
    from mavis.store.repo import messages, outbox

    async def no_names(uid):
        return set()

    hooks.DECISION_POLICIES.append(EmailTriage(no_names).apply_policy)
    monkeypatch.setattr(settings, "ping_daily_budget", 1)
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await messages.log(user.id, Role.ASSISTANT, "earlier ping", proactive=True)
    email = Event(id="gmail:msg:s1", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                  occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                  payload={"from": "no-reply@accounts.example", "subject": "Security alert: new sign-in",
                           "snippet": "", "message_id": "s1"})
    fake_llm.push_structured(InitiativeDecision(ignore_reason="meh"))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Was that new sign-in you?"]))
    await init.handler.handle(email)
    assert await outbox.texts_with_dedupe_prefix(f"email:{user.id}:s1:") == ["Was that new sign-in you?"]


async def test_model_cannot_claim_security(user, clock, recording_bus, fake_memory, fake_llm, settings,
                                           monkeypatch):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    monkeypatch.setattr(settings, "ping_daily_budget", 1)
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await messages.log(user.id, Role.ASSISTANT, "earlier ping", proactive=True)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent="hi", security=True)))
    await init.handler.handle(Event(id="wakeup:91", user_id=user.id, type=EventType.WAKEUP,
                                    occurred_at=timeutil.now(), source="timer",
                                    payload={"kind": "agent", "reason": "check", "wakeup_id": 91}))
    assert [m.content for m in await messages.recent(user.id) if m.proactive] == ["earlier ping"]


def test_security_flag_hidden_from_model_schema():
    assert "security" not in NotifyIntent.model_json_schema()["properties"]


async def test_security_notice_still_waits_for_quiet_hours(user, clock, settings, monkeypatch):
    from mavis.policy.pings import PingPolicy

    clock.set(ist(27, 2, 0))
    verdict = await PingPolicy().check(user, 4, "sec", timeutil.now(), bypass_budget=True)
    assert not verdict.allow and verdict.defer_until is not None


async def test_untrusted_ping_does_not_use_the_loop_slot(user, clock, recording_bus, fake_memory, fake_llm):
    from mavis.domain.decisions import ComposedMessage
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 0))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.WAITING_ON, title="Reply from Jawahar"))
    recording_bus.take()
    for i, trust in enumerate([Trust.UNTRUSTED, Trust.SYSTEM]):
        fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="nudge",
                                                                        dedupe_key=f"k{i}")))
        fake_llm.push_structured(ComposedMessage(send=True, messages=[f"nudge {i}"]))
        await init.handler.handle(Event(id=f"wakeup:{95 + i}", user_id=user.id, type=EventType.WAKEUP,
                                        occurred_at=timeutil.now(), source="timer", trust=trust,
                                        payload={"kind": "agent", "loop_id": loop.id, "reason": "r",
                                                 "wakeup_id": 95 + i}))
    assert [m.content for m in await messages.recent(user.id) if m.proactive] == ["nudge 0", "nudge 1"]


# review I1/I2 ------------------------------------------------------------------------------------

def ended_event(user, loop_id: int, eid: str = "wakeup:120") -> Event:
    return Event(id=eid, user_id=user.id, type=EventType.EVENT_ENDED, occurred_at=timeutil.now(),
                 source="timer", payload={"kind": "event_ended", "loop_id": loop_id, "wakeup_id": 120,
                                          "reason": "Follow up"})


async def _ended_loop(init, user, recording_bus):
    data = LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview with Jawahar", due_at=ist(27, 10, 0),
                      importance=5)
    loop = await init.loops.upsert(user.id, data)
    recording_bus.take()
    return loop


async def _drain(init, recording_bus):
    for event in recording_bus.take():
        await init.handler.handle(event)


async def test_follow_up_wakeup_survives_the_awaiting_transition(user, clock, recording_bus, fake_memory,
                                                                 fake_llm):
    from mavis.domain.decisions import ComposedMessage, WakeupRequest
    from mavis.domain.loops import LoopStatus
    from mavis.domain.wakeups import WakeupKind

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    loop = await _ended_loop(init, user, recording_bus)
    fake_llm.push_structured(InitiativeDecision(
        notify=NotifyIntent(urgency=3, intent="ask how it went"),
        wakeups=[WakeupRequest(at=ist(28, 10, 0), reason="Check the thank-you note to Jawahar went out",
                               loop_id=loop.id)]))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["How did the interview go?"]))
    await init.handler.handle(ended_event(user, loop.id))
    await _drain(init, recording_bus)  # LOOP_UPDATED for AWAITING cancels the loop's own wakeups
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY
    agent = await init.wakeups.pending(user.id, WakeupKind.AGENT)
    assert [w.reason for w in agent] == ["Check the thank-you note to Jawahar went out"]
    assert agent[0].loop_id is None


async def test_undelivered_follow_up_closes_the_loop(user, clock, recording_bus, fake_memory, fake_llm):
    from mavis.domain.decisions import ComposedMessage
    from mavis.domain.loops import LoopStatus

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    loop = await _ended_loop(init, user, recording_bus)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ask how it went")))
    fake_llm.push_structured(ComposedMessage(send=False))  # composer decided it is no longer relevant
    await init.handler.handle(ended_event(user, loop.id))
    assert (await init.loops.get(loop.id)).status is LoopStatus.DONE


async def test_deferred_follow_up_survives_a_reply_to_another_ping(user, clock, recording_bus, fake_memory,
                                                                   fake_llm):
    from mavis.domain.decisions import ComposedMessage
    from mavis.domain.loops import LoopStatus
    from mavis.domain.messages import Role
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import messages
    from mavis.timers.runner import wakeup_event

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 23, 30))  # quiet hours: the follow-up is deferred to 07:00
    data = LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview with Jawahar", due_at=ist(27, 21, 0),
                      importance=5)
    loop = await init.loops.upsert(user.id, data)
    recording_bus.take()
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ask how it went")))
    await init.handler.handle(ended_event(user, loop.id))
    await _drain(init, recording_bus)
    assert (await init.loops.get(loop.id)).status is LoopStatus.OPEN
    [deferred] = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)

    clock.set(ist(28, 6, 50))  # an unrelated proactive check-in, and the user answers it
    await messages.log(user.id, Role.ASSISTANT, "Morning! Big day?", proactive=True)
    clock.advance(minutes=2)
    await messages.log(user.id, Role.USER, "yes")
    assert await init.loops.on_user_message(user.id, "yes") == 0
    assert (await init.loops.get(loop.id)).status is LoopStatus.OPEN

    clock.set(deferred.due_at)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["How did last night's interview go?"]))
    await init.handler.handle(wakeup_event(deferred))
    await _drain(init, recording_bus)
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY
    clock.advance(minutes=5)
    await messages.log(user.id, Role.USER, "really well")
    assert await init.loops.on_user_message(user.id, "really well") == 1


async def test_direct_reply_to_another_ping_does_not_close_awaiting(user, clock, recording_bus, fake_memory):
    from mavis.domain.loops import LoopStatus
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 12, 0))
    await messages.log(user.id, Role.ASSISTANT, "How did it go?", proactive=True)
    loop = await _awaiting_loop(init, user, recording_bus)
    clock.advance(hours=3)
    await messages.log(user.id, Role.ASSISTANT, "Lunch plans?", proactive=True)
    clock.advance(minutes=1)
    await messages.log(user.id, Role.USER, "sure")
    assert await init.loops.on_user_message(user.id, "sure") == 0
    assert (await init.loops.get(loop.id)).status is LoopStatus.AWAITING_REPLY


# re-review R1 ------------------------------------------------------------------------------------

def _security_mail(user, i: int, **extra) -> Event:
    return Event(id=f"gmail:msg:r{i}", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                 occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                 payload={"from": "deals@shop.example", "subject": f"Unusual activity? Offer {i}",
                          "snippet": "", "message_id": f"r{i}", **extra})


async def _over_budget(init, user, settings, monkeypatch):
    from mavis.domain.messages import Role
    from mavis.initiative import hooks
    from mavis.initiative.email_triage import EmailTriage
    from mavis.store.repo import messages

    async def no_names(uid):
        return set()

    hooks.DECISION_POLICIES.append(EmailTriage(no_names).apply_policy)
    monkeypatch.setattr(settings, "ping_daily_budget", 1)
    await messages.log(user.id, Role.ASSISTANT, "earlier ping", proactive=True)


async def test_promo_security_wording_does_not_bypass_budget(user, clock, recording_bus, fake_memory,
                                                             fake_llm, settings, monkeypatch):
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await _over_budget(init, user, settings, monkeypatch)
    for i in range(8):
        fake_llm.push_structured(InitiativeDecision(ignore_reason="promo"))  # nothing reaches the composer
        await init.handler.handle(_security_mail(user, i, labels=["CATEGORY_PROMOTIONS"]))
    assert [m.content for m in await messages.recent(user.id) if m.proactive] == ["earlier ping"]


async def test_security_bypass_is_capped_at_two_then_deferred(user, clock, recording_bus, fake_memory,
                                                              fake_llm, settings, monkeypatch):
    from mavis.domain.decisions import ComposedMessage
    from mavis.domain.wakeups import WakeupKind
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await _over_budget(init, user, settings, monkeypatch)
    for i in range(4):
        fake_llm.push_structured(InitiativeDecision(ignore_reason="?"))
        if i < 2:  # only the capped two reach the composer
            fake_llm.push_structured(ComposedMessage(send=True, messages=[f"alert {i}"]))
        await init.handler.handle(_security_mail(user, i))
    sent = [m.content for m in await messages.recent(user.id) if m.proactive]
    assert sent == ["earlier ping", "alert 0", "alert 1"]
    deferred = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert len(deferred) == 2
    assert all(w.due_at == ist(28, 7, 0) for w in deferred)
    assert all(datetime.fromisoformat(w.payload["valid_until"]) > w.due_at for w in deferred)


# re-review R2 ------------------------------------------------------------------------------------

async def test_reschedule_email_cannot_produce_a_trusted_urgent_ping(user, clock, recording_bus, fake_memory,
                                                                     fake_llm, monkeypatch):
    from mavis.domain.loops import LoopUpsert as LU
    from mavis.domain.wakeups import WakeupKind
    from mavis.timers.runner import wakeup_event

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 20, 0))
    due = ist(28, 15, 0)
    data = LoopUpsert(kind=LoopKind.COMMITMENT, title="Interview with Jawahar", due_at=due, importance=4,
                      source="tg:update:1", trust=Trust.USER)
    loop = await init.loops.upsert(user.id, data)
    recording_bus.take()
    email = Event(id="gmail:msg:resched", user_id=user.id, type=EventType.EMAIL_RECEIVED,
                  occurred_at=timeutil.now(), source="composio", trust=Trust.UNTRUSTED,
                  payload={"from": "hr@fractal.example", "subject": "Interview rescheduled",
                           "snippet": "Now at 3:30 AM", "message_id": "resched"})
    fake_llm.push_structured(InitiativeDecision(track=[
        LU(id=loop.id, kind=LoopKind.COMMITMENT, title="Interview with Jawahar", due_at=ist(28, 3, 30),
           importance=5, entities=["HR"])]))
    await init.handler.handle(email)
    after = await init.loops.get(loop.id)
    assert after.due_at == due and after.importance == 4  # email cannot move or upgrade the loop
    assert after.entities == ["HR"] and not after.trusted  # the email's content taints the loop
    for event in recording_bus.take():
        await init.handler.handle(event)  # LOOP_UPDATED re-plans the derived signals
    [prep] = await init.wakeups.pending(user.id, WakeupKind.EVENT_STARTING)
    assert prep.due_at == due - timedelta(hours=1) and prep.payload.get("untrusted") is True

    calls = spy_notify(init, monkeypatch)
    clock.set(prep.due_at)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent="prep now")))
    await init.handler.handle(wakeup_event(prep))
    assert calls and calls[0]["untrusted"] is True and calls[0]["intent"].urgency <= 4


async def test_imminent_floor_requires_importance_four(user, clock, recording_bus, fake_memory, fake_llm,
                                                       monkeypatch):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 9, 30))
    loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Coffee",
                                                       due_at=ist(27, 10, 0), importance=3))
    recording_bus.take()
    calls = spy_notify(init, monkeypatch)
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="coffee soon")))
    await init.handler.handle(starting_event(user, loop.id))
    assert calls[0]["intent"].urgency == 3


# reminders (T10b; T11 ruling: user-requested reminders always fire) ------------------------------------

def reminder_event(user, eid: str = "wakeup:70", wid: int = 70, due=None, **extra) -> Event:
    return Event(id=eid, user_id=user.id, type=EventType.WAKEUP, occurred_at=due or timeutil.now(),
                 source="timer", trust=Trust.SYSTEM,
                 payload={"kind": "agent", "reminder": True, "wakeup_id": wid,
                          "reason": "Reminder the user asked for: stretch", **extra})


async def _proactive(user_id: int) -> list[str]:
    from mavis.store.repo import messages

    return [m.content for m in await messages.recent(user_id) if m.proactive]


async def test_reminder_sends_fixed_text_without_reasoner_or_composer(user, clock, recording_bus,
                                                                       fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await init.handler.handle(reminder_event(user))  # nothing scripted: any LLM call would raise
    assert fake_llm.calls == [] and await _proactive(user.id) == ["⏰ Reminder: stretch"]


async def test_identical_reminder_is_deduped(user, clock, recording_bus, fake_memory, fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    await init.handler.handle(reminder_event(user))
    await init.handler.handle(reminder_event(user))
    assert len(await _proactive(user.id)) == 1


async def test_reminder_fires_despite_the_daily_budget(user, clock, recording_bus, fake_memory, fake_llm,
                                                       settings, monkeypatch):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    monkeypatch.setattr(settings, "ping_daily_budget", 1)
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 14, 0))
    for i in range(3):
        await messages.log(user.id, Role.ASSISTANT, f"earlier ping {i}", proactive=True)
    await init.handler.handle(reminder_event(user))
    assert (await _proactive(user.id))[-1] == "⏰ Reminder: stretch"


async def test_reminder_in_quiet_hours_is_deferred_then_fires_late_with_a_note(user, clock, recording_bus,
                                                                                fake_memory, fake_llm):
    from mavis.domain.wakeups import WakeupKind
    from mavis.timers.runner import wakeup_event

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 23, 30))  # quiet hours, and the user has not written recently
    await init.handler.handle(reminder_event(user))
    assert await _proactive(user.id) == []
    [w] = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert w.payload["reminder"] is True and w.payload["reminder_key"] == "reminder:70"
    assert w.due_at == ist(28, 7, 0)
    clock.set(w.due_at)
    await init.handler.handle(wakeup_event(w))
    assert await _proactive(user.id) == ["⏰ Reminder, a bit late (it was for Sun 27 Sep, 23:30): stretch"]


async def test_reminder_in_quiet_hours_fires_when_the_user_is_awake(user, clock, recording_bus, fake_memory,
                                                                    fake_llm):
    from mavis.domain.messages import Role
    from mavis.store.repo import messages

    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 23, 30))
    await messages.log(user.id, Role.USER, "still up")
    await init.handler.handle(reminder_event(user))
    assert await _proactive(user.id) == ["⏰ Reminder: stretch"]


async def test_reminder_more_than_two_hours_late_is_still_delivered(user, clock, recording_bus, fake_memory,
                                                                    fake_llm):
    init = build(recording_bus, fake_memory)
    clock.set(ist(27, 15, 0))
    await init.handler.handle(reminder_event(user, due=ist(27, 12, 0)))  # the worker was down for 3h
    assert await _proactive(user.id) == ["⏰ Reminder, a bit late (it was for 12:00): stretch"]


async def test_reminder_flag_is_reserved_for_wake_me(user):
    import pytest

    from mavis.timers.service import WakeupService

    with pytest.raises(ValueError, match="reminder"):
        await WakeupService().wake_me(user.id, timeutil.now(), "x", payload={"reminder": True})
    wid = await WakeupService().wake_me(user.id, timeutil.now(), "x", reminder=True)
    [w] = [w for w in await WakeupService().pending(user.id) if w.id == wid]
    assert w.payload == {"reminder": True}
