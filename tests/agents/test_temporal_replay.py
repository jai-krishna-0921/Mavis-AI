"""T1: every place that replays stored messages to a model stamps each one relative to now, in the user's
zone; the current user message is not stamped; prompts say stamps are metadata and that relative words in
earlier text are relative to when it was written."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mavis.agents.simple_turn import run_turn
from mavis.domain.decisions import ComposedMessage, InitiativeDecision
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.messages import Role
from mavis.domain.timefmt import STAMP
from mavis.store.repo import messages, users

IST, NYC, LON, AKL = "Asia/Kolkata", "America/New_York", "Europe/London", "Pacific/Auckland"
ZONES = [IST, NYC, LON, AKL]


def local(tz: str, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 10, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def msg_event(user_id: int, text: str, event_id: str, at: datetime) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=at,
                 source="telegram", payload={"text": text}, trust=Trust.USER)


@pytest.mark.parametrize("tz", ZONES)
async def test_chat_replay_stamps_history_but_not_the_current_message(db, channel, fake_llm, memory, bus,
                                                                     clock, tz) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    await users.update(user.id, timezone=tz)
    clock.set(local(tz, 4, 15, 46))  # Sunday
    fake_llm.push_text("Today's the day, good luck!")
    await run_turn(msg_event(user.id, "interview is tomorrow", "e1", clock.t))
    clock.set(local(tz, 6, 10, 0))  # Tuesday
    fake_llm.push_text("Sure.")
    await run_turn(msg_event(user.id, "what's on today?", "e2", clock.t))
    prompt = fake_llm.calls[-1]
    replay = prompt[1:]
    assert isinstance(replay[0], HumanMessage) and isinstance(replay[1], AIMessage)
    assert replay[0].content == "[2 days ago, Sun 4 Oct 15:46] interview is tomorrow"
    assert replay[1].content.startswith("[2 days ago, Sun 4 Oct 15:46] ")
    assert replay[-1].content == "what's on today?"  # the message being answered: no stamp
    system = prompt[0].content
    assert "Tuesday 06 October 2026, 10:00" in system


async def test_proactive_last_message_is_stamped_too(db, channel, fake_llm, memory, bus, clock) -> None:
    from mavis.agents.turn_support import to_langchain

    user, _ = await users.get_or_create_by_chat(78, "Jai")
    clock.set(local(IST, 6, 8, 0))
    await messages.log(user.id, Role.USER, "hi")
    clock.set(local(IST, 6, 9, 0))
    await messages.log(user.id, Role.ASSISTANT, "Morning! Your dentist is today.", proactive=True)
    history = await messages.recent(user.id)
    out = to_langchain(history, local(IST, 7, 9, 0), IST)
    assert [m.content for m in out] == [
        "[yesterday, Tue 6 Oct 08:00] hi",
        "[yesterday, Tue 6 Oct 09:00] Morning! Your dentist is today.",
    ]


async def test_echoed_stamp_is_stripped_from_the_reply(db, channel, fake_llm, memory, bus, clock) -> None:
    user, _ = await users.get_or_create_by_chat(79, "Jai")
    clock.set(local(IST, 6, 10, 0))
    fake_llm.push_text("[just now] Hey there!")
    await run_turn(msg_event(user.id, "hey", "e1", clock.t))
    log = await messages.recent(user.id)
    assert log[-1].content == "Hey there!"


async def test_persona_states_the_stamp_and_relative_word_rules(db) -> None:
    from mavis.agents.persona import system_prompt

    user, _ = await users.get_or_create_by_chat(80, "Jai")
    prompt = system_prompt(user, local(IST, 6, 10, 0))
    assert "stamp" in prompt and "metadata" in prompt and "never write" in prompt.lower()
    assert "relative to when that text was written" in prompt
    assert "—" not in prompt and "–" not in prompt


async def test_reasoner_recent_conversation_is_stamped(user, clock, fake_memory, monkeypatch) -> None:
    from mavis.initiative.filters import FilterResult
    from mavis.initiative.reasoner import Reasoner
    from mavis.llm import models as llm
    from mavis.policy.pings import PingPolicy

    seen: dict = {}

    async def fake(schema, system, user_msg, **kw):
        seen.update(system=system, user=user_msg)
        return InitiativeDecision(ignore_reason="t")

    monkeypatch.setattr(llm, "structured", fake)
    await users.update(user.id, timezone=NYC)
    user = await users.get(user.id)
    clock.set(local(NYC, 3, 9, 0))
    await messages.log(user.id, Role.USER, "block 2pm tomorrow for the dentist")
    clock.set(local(NYC, 5, 7, 30))
    ev = Event(id="wakeup:1", user_id=user.id, type=EventType.WAKEUP, occurred_at=clock.t, source="timer",
               payload={"kind": "agent", "reason": "r"})
    await Reasoner(fake_memory, PingPolicy()).decide(
        user, ev, FilterResult(drop=False, relevance=0.5, summary="s", matched_loops=[]))
    assert "User: [2 days ago, Sat 3 Oct 09:00] block 2pm tomorrow for the dentist" in seen["user"]
    assert "relative to when that text was written" in seen["system"]


async def test_composer_recent_conversation_is_stamped(user, clock, fake_memory, fake_llm) -> None:
    from mavis.initiative.composer import Composer

    await users.update(user.id, timezone=AKL)
    user = await users.get(user.id)
    clock.set(local(AKL, 5, 23, 50))
    await messages.log(user.id, Role.USER, "gym tonight")
    clock.set(local(AKL, 6, 0, 5))
    fake_llm.push_structured(ComposedMessage(send=True, messages=["hi"]))
    await Composer(fake_memory).compose(user, "check in", 2)
    call = fake_llm.structured_calls[-1]
    assert "User: [yesterday, Mon 5 Oct 23:50] gym tonight" in call["user"]
    assert "relative to when that text was written" in call["system"]


async def test_summary_transcript_carries_absolute_times_and_asks_for_absolute_dates(db, fake_llm,
                                                                                      clock) -> None:
    from mavis.memory.summaries import maybe_summarize

    user, _ = await users.get_or_create_by_chat(111, "Jai")
    await users.update(user.id, timezone=LON)
    clock.set(local(LON, 1, 9, 0))
    for i in range(45):
        await messages.log(user.id, Role.USER if i % 2 == 0 else Role.ASSISTANT, f"message {i}")
        clock.advance(minutes=30)
    fake_llm.push_text("Summary.")
    assert await maybe_summarize(user.id) is True
    system, prompt = fake_llm.calls[-1][0].content, fake_llm.calls[-1][1].content
    assert "[Thu 1 Oct 2026 09:00] user: message 0" in prompt
    assert "absolute dates" in system and "never relative words" in system


async def test_summary_in_chat_context_says_when_it_was_written(db, channel, fake_llm, memory, bus,
                                                                clock) -> None:
    from mavis.store.repo import summaries

    user, _ = await users.get_or_create_by_chat(81, "Jai")
    clock.set(local(IST, 3, 20, 0))
    await summaries.add(user.id, 0, "Jai has an interview tomorrow.")
    clock.set(local(IST, 6, 10, 0))
    fake_llm.push_text("ok")
    await run_turn(msg_event(user.id, "hi", "e1", clock.t))
    system = fake_llm.calls[-1][0].content
    assert "## Earlier in our conversation (summary written 3 days ago, Sat 3 Oct 20:00)" in system


async def test_revise_prompt_carries_the_users_now(user, fake_llm, rec_bus, sent, fresh_registry,
                                                   clock) -> None:
    from mavis.agents.orchestrator_graph import revise_approval
    from mavis.store.repo import approvals
    from mavis.tools.integrations.tools import register_integration_tools

    await users.update(user.id, timezone=NYC)
    clock.set(local(NYC, 6, 22, 0))
    register_integration_tools(fresh_registry)
    tool = fresh_registry.get("calendar_create_event")
    aid = await approvals.create(user.id, None, "calendar_create_event",
                                 {"summary": "Sync", "start": "2026-10-07T11:00:00-04:00"}, "old",
                                 clock.t + timedelta(hours=48))
    fake_llm.push_structured(tool.args_model.model_validate({"summary": "Sync", "start": "2026-10-07T09:00"}))
    await revise_approval(await approvals.get(aid), "move it to 9 tomorrow")
    call = fake_llm.structured_calls[-1]
    assert "Tuesday 06 October 2026, 22:00" in call["system"] + call["user"]
    assert "America/New_York" in call["system"] + call["user"]


def test_stamp_grammar_is_what_replay_uses() -> None:
    from mavis.domain.timefmt import message_stamp

    assert STAMP.fullmatch(message_stamp(local(IST, 1, 9, 0), local(IST, 6, 9, 0), IST))
