"""Phase 3 demo, pinned: chat -> LEARN -> loop -> agent-owned wakeups -> pep talk -> "how'd it go?".

Real wiring throughout: register_default_handlers, the in-process bus consumers, MemoryService (fake
embeddings), LoopService, WakeupService, TimerRunner.tick, executor, PingPolicy and the outbox sender.
Only the LLM (fake_llm) and the clock are scripted.
"""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mavis.channels.outbox_sender import deliver_pending
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, NotifyIntent
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.loops import LoopStatus
from mavis.domain.memory import ExtractedEvent, Extraction
from mavis.domain.messages import Role
from mavis.domain.wakeups import WakeupKind
from mavis.initiative import wiring
from mavis.store.repo import messages
from mavis.worker.handlers import register_default_handlers
from mavis.worker.runner import handle_event, handle_job

IST = ZoneInfo("Asia/Kolkata")
DASHES = re.compile("[–—]")
PEP_INTENT = "pep talk before prep"
PEP_TEXT = "Big day! Prep with Jawahar at 10. You've got this"
FOLLOW_UP_INTENT = "ask how the prep went"
FOLLOW_UP_TEXT = "How'd the interview prep with Jawahar go?"


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST).astimezone(UTC)


@asynccontextmanager
async def running(bus):
    """The worker's event and job consumers, running until the bus is idle."""
    seen: list[Event] = []

    async def on_event(event: Event) -> None:
        seen.append(event)
        await handle_event(event)

    tasks = [asyncio.create_task(bus.consume_events("workers", "t", on_event)),
             asyncio.create_task(bus.consume_jobs("workers", "t", handle_job))]
    try:
        yield seen
        await asyncio.wait_for(bus.wait_idle(), timeout=20)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not bus.dead_events and not bus.dead_jobs


@pytest.fixture
async def world(user, clock, bus, memory, fake_llm, channel):
    clock.set(ist(26, 15, 0))  # Saturday 15:00 IST
    register_default_handlers()
    return wiring.current()


def push_extraction_for_interview(fake_llm) -> None:
    fake_llm.push_structured(Extraction(events=[ExtractedEvent(
        title="Interview prep with Jawahar", starts_at=ist(28, 10, 0), with_people=["Jawahar"],
        importance=5)]))


async def say_interview(user, bus, fake_llm, clock):
    push_extraction_for_interview(fake_llm)
    fake_llm.push_text("Nice, Monday 10am with Jawahar. Want to prep tonight?")
    # The reasoner sees LOOP_CREATED for the new loop and is happy with the default plan.
    fake_llm.push_structured(InitiativeDecision(reasoning="defaults are right", ignore_reason="default plan"))
    async with running(bus) as seen:
        await bus.publish(Event(
            id="tg:update:42", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=clock.t,
            source="telegram", payload={"text": "interview prep with Jawahar Monday 10am"},
            trust=Trust.USER))
    # The turn's LEARN is parked until just after the LLM interactive grace window (hotfix3): let the
    # timer fire its system_learn wakeup, then extraction creates the loop.
    clock.advance(seconds=30)
    async with running(bus) as later:
        assert await wiring.current().timer.tick() == 1
    return seen + later


async def test_interview_prep_pep_talk_then_follow_up(user, world, clock, bus, fake_llm, channel):
    init = world
    await say_interview(user, bus, fake_llm, clock)
    loops = [lp for lp in await init.loops.active(user.id) if lp.title == "Interview prep with Jawahar"]
    assert len(loops) == 1
    pending = [w for w in await init.wakeups.pending(user.id) if w.loop_id == loops[0].id]
    assert sorted(w.kind for w in pending) == [WakeupKind.EVENT_ENDED, WakeupKind.EVENT_STARTING]
    by_kind = {w.kind: w for w in pending}
    assert by_kind[WakeupKind.EVENT_STARTING].due_at == ist(28, 9, 0)
    assert by_kind[WakeupKind.EVENT_ENDED].due_at == ist(28, 12, 0)
    loop_id = loops[0].id

    # Isolate the interview flow: the weekend morning check-in and the "user went quiet" nudge have
    # their own tests and would otherwise fire during the jump to Monday.
    await init.wakeups.cancel_where(user.id, [WakeupKind.ROUTINE, WakeupKind.USER_QUIET])

    # Mon 09:00 IST: the prep wakeup fires; Mavis sends a pep talk unprompted.
    clock.set(ist(28, 9, 0))
    assert await init.timer.tick() == 1
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent=PEP_INTENT)))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[PEP_TEXT]))
    async with running(bus) as seen:
        pass
    await deliver_pending(channel)
    assert any("You've got this" in t for t in channel.texts)
    sent_before = list(channel.texts)

    # The bus redelivers the same wakeup event (consumer crashed after handling): no second pep talk.
    wake = next(e for e in seen if e.id.startswith("wakeup:"))
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=4, intent=PEP_INTENT)))
    await handle_event(wake)
    await deliver_pending(channel)
    assert channel.texts == sent_before
    assert not any("how" in t.lower() and "go" in t.lower() for t in channel.texts)

    # Mon 12:00 IST: the follow-up wakeup fires; Mavis asks how it went and closes the loop.
    clock.set(ist(28, 12, 0))
    assert await init.timer.tick() == 1
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent=FOLLOW_UP_INTENT)))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[FOLLOW_UP_TEXT]))
    async with running(bus):
        pass
    await deliver_pending(channel)
    assert FOLLOW_UP_TEXT in channel.texts

    # QA: asking is not closing; the loop waits for the user's answer, which closes it.
    assert all(lp.id != loop_id for lp in await init.loops.active(user.id))
    assert (await init.loops.get(loop_id)).status is LoopStatus.AWAITING_REPLY
    await messages.log(user.id, Role.USER, "it went really well!")
    assert await init.loops.on_user_message(user.id, "it went really well!") == 1
    closed = await init.loops.get(loop_id)
    assert closed is not None and closed.status is LoopStatus.DONE
    # F3: proactive messages never arm a went-quiet nudge, even when they end with a question.
    assert len(await init.wakeups.pending(user.id, WakeupKind.USER_QUIET)) == 0
    assert not any(DASHES.search(t) for t in channel.texts)
    assert await deliver_pending(channel) == 0  # nothing left to send


async def test_wakeup_in_quiet_hours_is_deferred_to_morning(user, world, clock, bus, fake_llm, channel):
    init = world
    await init.wakeups.cancel_where(user.id, [WakeupKind.ROUTINE, WakeupKind.USER_QUIET])
    clock.set(ist(28, 22, 0))
    await init.wakeups.wake_me(user.id, ist(28, 23, 30), "late nudge", kind=WakeupKind.AGENT,
                               dedupe_key="e2e:late")

    clock.set(ist(28, 23, 30))
    assert await init.timer.tick() == 1
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="late nudge")))
    async with running(bus):
        pass
    await deliver_pending(channel)
    assert channel.texts == []  # 23:30 IST is inside quiet hours: nothing sent, no composer call
    deferred = await init.wakeups.pending(user.id, WakeupKind.DEFERRED)
    assert [w.due_at for w in deferred] == [ist(29, 7, 0)]

    clock.set(ist(29, 7, 0))
    assert await init.timer.tick() == 1
    fake_llm.push_structured(ComposedMessage(send=True, messages=["Morning! Quick nudge from last night."]))
    async with running(bus):
        pass
    await deliver_pending(channel)
    assert channel.texts == ["Morning! Quick nudge from last night."]
