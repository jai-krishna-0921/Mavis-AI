from datetime import UTC, datetime

from mavis.agents import clarify, simple_turn
from mavis.channels.outbox_sender import deliver_pending
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.memory import Extraction
from mavis.llm import models as llm
from mavis.store.repo import messages, users

AFTER_MIDNIGHT_IST = datetime(2026, 9, 27, 18, 39, tzinfo=UTC)  # Mon 28 Sep 00:09 IST


def _event(user_id: int, text: str, event_id: str) -> Event:
    return Event(
        id=event_id, user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
        source="telegram", payload={"text": text}, trust=Trust.USER,
    )


def test_day_clarification_uses_user_timezone(clock):
    clock.set(AFTER_MIDNIGHT_IST)
    assert clarify.day_clarification("tomorrow 10am", "Asia/Kolkata") is not None
    # Same instant is 14:39 the previous day in New York: not ambiguous.
    assert clarify.day_clarification("tomorrow 10am", "America/New_York") is None


def test_question_has_no_em_or_en_dashes(clock):
    clock.set(AFTER_MIDNIGHT_IST)
    question = clarify.day_clarification("tomorrow 10am", "Asia/Kolkata")
    assert question is not None
    assert "—" not in question and "–" not in question
    assert clarify.is_day_question(question)


async def test_ambiguous_tomorrow_asks_before_llm(db, clock, channel, fake_llm, memory, bus):
    clock.set(AFTER_MIDNIGHT_IST)
    user, _ = await users.get_or_create_by_chat(1001, "Jai")
    user = await users.get(user.id)
    assert user.timezone == "Asia/Kolkata"
    event = _event(user.id, "Plan a meeting for tomorrow at 10 am", "tg:update:1")
    await simple_turn.run_turn(event)  # fake_llm queue is empty: any LLM call would raise
    await deliver_pending(channel)
    assert any("Monday Sep 28" in text and "Tuesday Sep 29" in text for text in channel.texts)
    assert fake_llm.structured_calls == []
    log = await messages.recent(user.id)
    assert [m.role for m in log] == ["user", "assistant"]


async def test_answer_turn_learns_the_original_request(
    db, clock, channel, fake_llm, memory, bus, monkeypatch
):
    jobs = []

    async def record(job):
        jobs.append(job)

    monkeypatch.setattr(bus, "enqueue", record)
    clock.set(AFTER_MIDNIGHT_IST)
    user, _ = await users.get_or_create_by_chat(1001, "Jai")
    await simple_turn.run_turn(_event(user.id, "Plan a meeting for tomorrow at 10 am", "tg:update:1"))
    fake_llm.push_text("Got it, Tuesday at 10.")
    await simple_turn.run_turn(_event(user.id, "Tuesday", "tg:update:2"))
    learn = next(j for j in jobs if j.id == "learn:tg:update:2")
    assert learn.payload["text"].startswith("User: Plan a meeting for tomorrow at 10 am\nMavis: ")
    assert learn.payload["text"].endswith("User: Tuesday")


async def test_extractor_prompt_flags_midnight_ambiguity(db, clock, memory, monkeypatch):
    clock.set(AFTER_MIDNIGHT_IST)
    user, _ = await users.get_or_create_by_chat(1001, "Jai")
    seen: dict[str, str] = {}

    async def capture(schema, system, user_msg, tier=llm.Tier.FAST):
        if schema is Extraction:
            seen["system"] = system
            return Extraction()
        return schema.model_construct()

    monkeypatch.setattr(llm, "structured", capture)
    await memory.learn(user.id, "Plan a meeting tomorrow at 10", "tg:1")
    assert "ambiguous=true" in seen["system"]
    # The prompt is anchored to the injectable clock, in the user's local time.
    assert "Monday 2026-09-28 00:09" in seen["system"]
    assert "04:59" in seen["system"]


async def test_replying_tomorrow_to_the_question_is_not_asked_again(
    db, clock, channel, fake_llm, memory, bus, monkeypatch
):
    jobs = []

    async def record(job):
        jobs.append(job)

    monkeypatch.setattr(bus, "enqueue", record)
    clock.set(AFTER_MIDNIGHT_IST)
    user, _ = await users.get_or_create_by_chat(1001, "Jai")
    await simple_turn.run_turn(_event(user.id, "Plan a meeting for tomorrow at 10 am", "tg:update:1"))
    clock.advance(minutes=3)
    fake_llm.push_text("Done, Tuesday at 10.")
    await simple_turn.run_turn(_event(user.id, "tomorrow please", "tg:update:2"))
    await deliver_pending(channel)
    assert len(channel.texts) == 2
    assert channel.texts[1] == "Done, Tuesday at 10."
    learn = next(j for j in jobs if j.id == "learn:tg:update:2")
    assert learn.payload["text"].startswith("User: Plan a meeting for tomorrow at 10 am\n")


def test_day_after_tomorrow_is_not_flagged(clock):
    clock.set(AFTER_MIDNIGHT_IST)
    assert clarify.day_clarification("the day after tomorrow at 10", "Asia/Kolkata") is None


def test_window_uses_message_time_not_now(clock):
    clock.set(datetime(2026, 9, 28, 8, 0, tzinfo=UTC))  # 13:30 IST now, but the message was sent at 00:09
    assert clarify.day_clarification("tomorrow", "Asia/Kolkata", AFTER_MIDNIGHT_IST) is not None
