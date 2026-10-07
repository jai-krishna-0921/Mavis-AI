"""H5: the ping composer is grounded in the subject's source record; history is claims."""

from datetime import timedelta

import pytest

from mavis.attention.schema import AttentionDecision, Verdict
from mavis.attention.speaker import Speaker
from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType
from mavis.domain.loops import LoopKind, LoopOrigin, LoopUpsert
from mavis.domain.messages import Role
from mavis.domain.tasks import TaskStatus
from mavis.initiative import recent_failures
from mavis.initiative.composer import Composer
from mavis.initiative.filters import FilterResult
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.wiring import build_initiative
from mavis.policy.pings import PingPolicy
from mavis.store.repo import attention, messages, tasks
from mavis.timers.service import WakeupService


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


async def make_obs(user, mid, sender, domain, summary, kind="security"):
    obs, _ = await attention.insert_pending(user.id, mid, thread_id="", origin="live", sender_domain=domain,
                                            sender_name=sender, received_at=timeutil.now(), payload={})
    await attention.finish(obs.id, kind=kind, verdict="notify", urgency=3, summary=summary,
                           facts={"codes": []})
    return await attention.get(obs.id)


@pytest.mark.parametrize(("sender", "domain", "summary"), [
    ("ICICI Lombard", "icicilombard.example", "one-time code for your policy login"),
    ("City Water Board", "water.example", "bill due on the 12th"),
    ("Acme Courier", "courier.example", "parcel held at the depot"),
])
async def test_observation_ping_composes_from_its_own_record(user, clock, recording_bus, fake_memory,
                                                             fake_llm, sender, domain, summary):
    """An older, different alert in the history must not stand in for this one: the record is in the
    prompt, marked authoritative, and the history is marked as claims."""
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    await messages.log(user.id, Role.ASSISTANT, "You've got a security alert from Google.", proactive=True)
    clock.advance(hours=30)
    obs = await make_obs(user, f"m-{domain}", sender, domain, summary)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    speaker = Speaker(lambda: init.executor, PingPolicy(), WakeupService())
    await speaker.speak(user, obs, AttentionDecision(Verdict.NOTIFY, 3, 0.8, ("new",)))
    call = fake_llm.structured_calls[-1]
    assert sender in call["user"] and summary in call["user"] and domain in call["user"]
    assert call["user"].index("Source record") < call["user"].index("Recent conversation")
    assert "claims" in call["system"] and "record wins" in call["system"]


@pytest.mark.parametrize("subject_kind", ["loop", "task"])
async def test_item_ping_carries_its_computed_state(user, clock, recording_bus, fake_memory, fake_llm,
                                                    subject_kind):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    if subject_kind == "loop":
        loop = await init.loops.upsert(user.id, LoopUpsert(kind=LoopKind.COMMITMENT, title="Block 2 to 3 PM",
                                                           due_at=timeutil.now() - timedelta(days=1),
                                                           origin=LoopOrigin.CONVERSATION))
        subject, expect = f"loop:{loop.id}", ["Block 2 to 3 PM", "Status: OPEN", "overdue"]
    else:
        tid = await tasks.create(user.id, "Research standing desks")
        await tasks.set_status(tid, TaskStatus.FAILED)
        subject, expect = f"task:{tid}", ["Research standing desks", "Status: failed"]
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    await init.executor.notify(user, NotifyIntent(urgency=3, intent="nudge", dedupe_key="x"),
                               origin={"kind": "wakeup", "subject": subject})
    prompt = fake_llm.structured_calls[-1]["user"]
    for piece in expect:
        assert piece.lower() in prompt.lower()


async def test_without_a_subject_there_is_no_record_block(user, clock, fake_memory, fake_llm):
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    await Composer(fake_memory).compose(user, "say hi", 2)
    assert "Source record" not in fake_llm.structured_calls[-1]["user"]


async def test_recently_failed_hook_reaches_composer_and_reasoner(user, clock, fake_memory, fake_llm):
    """Nothing failed: no section. A provider with lines reaches both prompts."""
    fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
    await Composer(fake_memory).compose(user, "say hi", 2)
    assert recent_failures.HEADING not in fake_llm.structured_calls[-1]["user"]

    async def provider(user_id):
        return "- Calendar block Sun 4 Oct 14:00: failed (invalid attendee)", False

    recent_failures.set_provider(provider)
    try:
        fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
        await Composer(fake_memory).compose(user, "say hi", 2)
        assert "failed (invalid attendee)" in fake_llm.structured_calls[-1]["user"]
        fake_llm.push_structured(InitiativeDecision())
        ev = Event(id="w", user_id=user.id, type=EventType.WAKEUP, occurred_at=timeutil.now(), source="timer")
        await Reasoner(fake_memory, PingPolicy()).decide(user, ev, FilterResult(drop=False, summary="s"))
        assert recent_failures.HEADING in fake_llm.structured_calls[-1]["user"]
    finally:
        recent_failures.set_provider(None)


async def test_a_failing_provider_never_blocks_a_ping(user, clock, fake_memory, fake_llm):
    async def broken(user_id):
        raise RuntimeError("db down")

    recent_failures.set_provider(broken)
    try:
        fake_llm.push_structured(ComposedMessage(send=True, messages=["ok"]))
        assert (await Composer(fake_memory).compose(user, "say hi", 2)).send
    finally:
        recent_failures.set_provider(None)
