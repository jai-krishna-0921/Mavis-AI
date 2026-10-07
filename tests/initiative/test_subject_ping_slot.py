"""H5: one proactive ping per subject per local day, reserved atomically before composing."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from mavis.domain import timeutil
from mavis.domain.decisions import ComposedMessage, NotifyIntent
from mavis.domain.errors import LLMError
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import Role
from mavis.initiative.wiring import build_initiative
from mavis.store.repo import messages

SUBJECTS = ["loop:5", "task:12", "approval:3", "observation:40"]


async def no_embed(texts):
    return [[1.0, 0.0] for _ in texts]


def ist(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC) - timedelta(hours=5, minutes=30)


async def sent(user) -> list[str]:
    return [m.content for m in await messages.recent(user.id, 50) if m.role == Role.ASSISTANT and m.proactive]


def intent(key: str) -> NotifyIntent:
    return NotifyIntent(urgency=3, intent="nudge", dedupe_key=key)


@pytest.mark.parametrize("subject", SUBJECTS)
async def test_second_ping_about_a_subject_under_another_key_is_skipped(user, clock, recording_bus,
                                                                        fake_memory, fake_llm, subject):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 11))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["first"]))
    origin = {"kind": "wakeup", "subject": subject}
    assert await init.executor.notify(user, intent("approve23pm"), origin=origin)
    # a different trigger, a different model key, the same subject: no compose, nothing sent
    assert not await init.executor.notify(user, intent("approval23pm04oct"),
                                          origin={"kind": "loop_created", "subject": subject})
    assert await sent(user) == ["first"]
    clock.set(ist(28, 11))  # the next local day the subject may come up again
    fake_llm.push_structured(ComposedMessage(send=True, messages=["next day"]))
    assert await init.executor.notify(user, intent("other"), origin={"kind": "wakeup", "subject": subject})


async def test_concurrent_pings_about_one_subject_compose_once(user, clock, recording_bus, fake_memory,
                                                               fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 15))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["only once"]))  # a second compose raises
    results = await asyncio.gather(
        init.executor.notify(user, intent("k1"), origin={"kind": "wakeup", "subject": "loop:9"}),
        init.executor.notify(user, intent("k2"), origin={"kind": "loop_created", "subject": "loop:9"}),
    )
    assert sorted(results) == [False, True]
    assert await sent(user) == ["only once"]


@pytest.mark.parametrize("failure", ["dropped", "error"])
async def test_a_failed_compose_releases_the_slot(user, clock, recording_bus, fake_memory, fake_llm, failure):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 16))
    origin = {"kind": "wakeup", "subject": "task:4"}
    if failure == "dropped":
        fake_llm.push_structured(ComposedMessage(send=False))
        assert not await init.executor.notify(user, intent("a"), origin=origin)
    else:
        fake_llm.push_error(LLMError("busy"), structured=True)
        with pytest.raises(LLMError):
            await init.executor.notify(user, intent("a"), origin=origin)
    fake_llm.push_structured(ComposedMessage(send=True, messages=["went out later"]))
    assert await init.executor.notify(user, intent("b"), origin=origin)
    assert await sent(user) == ["went out later"]


async def test_prep_and_follow_up_for_one_item_are_separate_slots(user, clock, recording_bus, fake_memory,
                                                                  fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 9))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["prep"]))
    prep = {"kind": "event_starting", "subject": "loop:2"}
    assert await init.executor.notify(user, intent("p"), origin=prep)
    clock.set(ist(27, 13))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["how did it go"]))
    assert await init.executor.notify(user, intent("f"), origin={"kind": "event_ended", "subject": "loop:2"})


async def test_different_subjects_each_get_their_slot(user, clock, recording_bus, fake_memory, fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 12))
    for i, subject in enumerate(SUBJECTS):
        fake_llm.push_structured(ComposedMessage(send=True, messages=[f"m{i}"]))
        origin = {"kind": "wakeup", "subject": subject}
        assert await init.executor.notify(user, intent(f"k{i}"), origin=origin)


async def test_untrusted_ping_does_not_use_the_trusted_slot(user, clock, recording_bus, fake_memory,
                                                            fake_llm):
    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 12))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["from an email"]))
    assert await init.executor.notify(user, intent("u"), untrusted=True,
                                      origin={"kind": "wakeup", "subject": "loop:8"})
    fake_llm.push_structured(ComposedMessage(send=True, messages=["trusted"]))
    assert await init.executor.notify(user, intent("t"), origin={"kind": "wakeup", "subject": "loop:8"})


async def test_handler_pings_carry_the_events_subject(user, clock, recording_bus, fake_memory, fake_llm):
    """Two wakeups about one task under different model keys: the second is skipped by subject."""
    from mavis.domain.decisions import InitiativeDecision
    from mavis.store.repo import tasks

    init = build_initiative(recording_bus, fake_memory, embed=no_embed)
    clock.set(ist(27, 14))
    tid = await tasks.create(user.id, "Find a plumber")
    for i, key in enumerate(["plumber_check", "plumber_followup_today"]):
        fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="plumber",
                                                                        dedupe_key=key)))
        if i == 0:
            fake_llm.push_structured(ComposedMessage(send=True, messages=["Any luck with the plumber?"]))
        await init.handler.handle(Event(id=f"wakeup:{900 + i}", user_id=user.id, type=EventType.WAKEUP,
                                        occurred_at=timeutil.now(), source="timer", trust=Trust.SYSTEM,
                                        payload={"kind": "agent", "reason": "r", "wakeup_id": 900 + i,
                                                 "subject": f"task:{tid}"}))
    assert await sent(user) == ["Any luck with the plumber?"]
