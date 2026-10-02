from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from zento.agents.simple_turn import run_turn
from zento.channels.outbox_sender import OutboxSender
from zento.domain.errors import LLMError
from zento.domain.events import Event, EventType, Trust
from zento.store.db import utcnow
from zento.store.repo import messages, outbox, users


def msg_event(user_id: int, text: str, event_id: str = "tg:update:1", **payload) -> Event:
    return Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=datetime.now(UTC),
                 source="telegram", payload={"text": text, **payload}, trust=Trust.USER)


async def test_run_turn_replies_in_bubbles_and_logs(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Hey Jai!\n\nWhat's on your plate today?")
    await run_turn(msg_event(user.id, "hi"))

    assert channel.sent[0].kind == "typing"
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Hey Jai!", "What's on your plate today?"]

    log = await messages.recent(user.id)
    assert [(m.role, m.content) for m in log] == [
        ("user", "hi"), ("assistant", "Hey Jai!\n\nWhat's on your plate today?"),
    ]
    prompt = fake_llm.calls[-1]
    assert isinstance(prompt[0], SystemMessage) and "You are Mavis" in prompt[0].content
    assert isinstance(prompt[-1], HumanMessage) and prompt[-1].content == "hi"


async def test_history_is_included(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Noted.")
    await run_turn(msg_event(user.id, "my friend Jawahar is helping me", "e1"))
    fake_llm.push_text("Jawahar!")
    await run_turn(msg_event(user.id, "who's helping me?", "e2"))
    contents = [m.content for m in fake_llm.calls[-1][1:]]
    assert contents == ["my friend Jawahar is helping me", "Noted.", "who's helping me?"]


async def test_start_command_adds_greeting_hint(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, None)
    fake_llm.push_text("Hey! I'm Mavis.")
    await run_turn(msg_event(user.id, "/start", command="start"))
    assert "just opened the chat" in fake_llm.calls[-1][0].content


async def test_file_message_is_described(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Got it.")
    await run_turn(msg_event(user.id, "", file={"file_id": "f", "file_name": "cv.pdf"}))
    assert fake_llm.calls[-1][-1].content == "[sent a file: cv.pdf]"


async def test_run_turn_is_idempotent_on_retry(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("First\n\nSecond")
    fake_llm.push_text("Other\n\nWords")  # a retry gets a different completion
    event = msg_event(user.id, "hi")
    await run_turn(event)
    await run_turn(event)
    assert [r.text for r in await outbox.due(utcnow())] == ["First", "Second"]
    assert [m.role for m in await messages.recent(user.id)] == ["user", "assistant"]


async def test_llm_error_propagates(db, channel, fake_llm) -> None:
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_error(TimeoutError())
    with pytest.raises(LLMError):
        await run_turn(msg_event(user.id, "hi"))
