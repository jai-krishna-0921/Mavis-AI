"""T1.2 end to end through the chat turn: the measured register reaches the model, slurs never leave."""

import pytest

from mavis.agents.simple_turn import run_turn
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.store.repo import users


def _event(user_id: int, text: str, n: int) -> Event:
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source="telegram", payload={"text": text}, trust=Trust.USER)


async def _turns(user_id, fake_llm, texts):
    for i, text in enumerate(texts):
        fake_llm.push_text("ok")
        await run_turn(_event(user_id, text, i))
    return fake_llm.calls[-1][0].content


@pytest.mark.parametrize("texts,expect,absent", [
    (["fuck it, book the cheap flight"], "casual and sweary", "formal"),
    (["you're fucking useless lol"], "casual and sweary", "formal"),
    (["hey", "lol this shit again", "ok wait"], "casual and sweary", "formal"),
    (["can u check my mail", "cool thx"], "don't swear: they haven't", "sweary"),
    (["fuck this week", "Good afternoon. Could you please summarise my inbox for me?"], "formal",
     "casual and sweary"),
])
async def test_register_line_reaches_the_model(db, channel, fake_llm, memory, bus, texts, expect, absent):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    system = await _turns(user.id, fake_llm, texts)
    assert expect in system
    assert f"Their register right now: {absent}" not in system


async def test_profanity_only_message_gets_a_normal_model_turn(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Ha, rough day? What happened?")
    await run_turn(_event(user.id, "fuck fuck FUCK", 1))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Ha, rough day? What happened?"]  # no moderation layer in the way


async def test_chat_reply_slurs_are_masked(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("That guy sounds like a total retard, honestly.")
    await run_turn(_event(user.id, "my landlord ignored me again", 1))
    await OutboxSender(channel).run_once()
    assert "retard" not in channel.texts[0]


@pytest.mark.parametrize("texts", [
    ["lol this week is shit", "fuck. my dog died this morning"],  # upset: swearing off
    ["this shit is broken lol", "Good afternoon. Could you please summarise the budget discussion?"],
    ["can u check my mail", "cool thx"],  # never swore
])
async def test_unmirrored_swearing_is_rewritten_once(db, channel, fake_llm, memory, bus, texts):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    for i, text in enumerate(texts[:-1]):
        fake_llm.push_text("ok")
        await run_turn(_event(user.id, text, i))
    fake_llm.push_text("That's not a shit week, that's awful.\n[react: \U0001f525]")
    fake_llm.push_text("That's not just a bad week, that's awful.")  # the rewrite
    await run_turn(_event(user.id, texts[-1], 99))
    await OutboxSender(channel).run_once()
    assert channel.texts[-1] == "That's not just a bad week, that's awful."
    rewrite = fake_llm.calls[-1]
    assert "no swearing" in rewrite[0].content.lower() and "shit week" in rewrite[-1].content
    assert "[react" not in rewrite[-1].content


async def test_mirrored_swearing_is_left_alone(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Fuck yes, congrats!")
    await run_turn(_event(user.id, "fuck yeah got the job", 1))  # a rewrite would hit an empty queue
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Fuck yes, congrats!"]


async def test_failed_rewrite_keeps_the_reply(db, channel, fake_llm, memory, bus):
    from mavis.domain.errors import LLMError

    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Damn, sorry. Is he stable?")
    fake_llm.push_error(LLMError("down"))
    await run_turn(_event(user.id, "my dad is in the hospital", 1))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Damn, sorry. Is he stable?"]
