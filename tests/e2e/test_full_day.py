"""A realistic day for an existing, onboarded user who ignores everything Mavis sends.

No wakeup is cancelled: the morning check-in, the prep reminder, the follow-up and any went-quiet
nudge all compete on one daily budget (final review: I3, Minor 7).
"""

from datetime import timedelta

from mavis.channels.outbox_sender import deliver_pending
from mavis.config import get_settings
from mavis.domain.decisions import ComposedMessage, InitiativeDecision, NotifyIntent
from mavis.domain.wakeups import WakeupKind
from mavis.initiative.wiring import current
from mavis.policy.pings import PingPolicy
from mavis.store.db import Session
from mavis.store.models import Message
from mavis.store.repo import users
from mavis.worker.handlers import register_default_handlers
from tests.e2e.test_interview_followup import DASHES, ist, running, say_interview

MORNING_TEXT = "Morning! Interview prep with Jawahar at 10. What else is on your plate?"
PEP = "Big day, prep with Jawahar at 10. You've got this"
FOLLOW = "How did the prep with Jawahar go?"


async def test_ignored_day_stays_within_budget_and_quiet_hours(user, clock, bus, memory, fake_llm, channel):
    register_default_handlers()
    init = current()
    await users.update(user.id, onboarded=True)
    async with Session() as s:  # Jai has chatted around 10:00 IST all week: learned check-in 09:30
        for day in (21, 22, 23, 24):
            s.add(Message(user_id=user.id, role="user", content="hi", proactive=False,
                          created_at=ist(day, 10, 0)))
        await s.commit()

    clock.set(ist(27, 21, 0))  # Sunday evening
    await say_interview(user, bus, fake_llm, clock)  # reply ends with "?" but Jai is not new any more
    await deliver_pending(channel)
    assert await init.wakeups.pending(user.id, WakeupKind.USER_QUIET) == []
    [morning] = await init.wakeups.pending(user.id, WakeupKind.ROUTINE)
    assert morning.due_at == ist(28, 9, 30)  # learned time, not the 08:30 default
    baseline = len(channel.texts)

    # Responses in the order Monday's wakeups will consume them: prep, morning, follow-up.
    fake_llm.push_structured(
        InitiativeDecision(notify=NotifyIntent(urgency=4, intent="pep talk before prep")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[PEP]))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[MORNING_TEXT]))
    fake_llm.push_structured(InitiativeDecision(notify=NotifyIntent(urgency=3, intent="ask how it went")))
    fake_llm.push_structured(ComposedMessage(send=True, messages=[FOLLOW]))

    t = ist(27, 21, 30)
    sent_at: list = []
    seen_texts = baseline
    while t <= ist(29, 7, 30):  # step through the day, user silent the whole time
        clock.set(t)
        await init.timer.tick()
        async with running(bus):
            pass
        await deliver_pending(channel)
        if len(channel.texts) > seen_texts:
            sent_at.append((t, channel.texts[seen_texts:]))
            seen_texts = len(channel.texts)
        t += timedelta(minutes=30)

    unsolicited = [txt for _, texts in sent_at for txt in texts]
    assert unsolicited == [PEP, MORNING_TEXT, FOLLOW]  # prep 09:00, check-in 09:30, follow-up 12:00
    assert [when for when, _ in sent_at] == [ist(28, 9, 0), ist(28, 9, 30), ist(28, 12, 0)]
    assert len(unsolicited) <= get_settings().ping_daily_budget
    assert await PingPolicy().count_today(user, ist(28, 20, 0)) == 3
    for when, _ in sent_at:  # nothing inside quiet hours 23:00-07:00 (local)
        local_hour = (when + timedelta(hours=5, minutes=30)).hour
        assert 7 <= local_hour < 23
    # No went-quiet nudge was ever scheduled or fired after the ignored questions.
    assert await init.wakeups.pending(user.id, WakeupKind.USER_QUIET) == []
    assert not any(DASHES.search(txt) for txt in channel.texts)
    assert not fake_llm.structured_queue  # every scripted LLM response was consumed
    assert not any(w.kind is WakeupKind.DEFERRED for w in await init.wakeups.pending(user.id))
