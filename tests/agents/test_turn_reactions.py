"""T1.3 through the chat turn: the model's reaction marker never reaches the text, code lands it."""

from mavis.agents.simple_turn import run_turn
from mavis.channels.outbox_sender import OutboxSender
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.store.repo import messages, users

FIRE, CLAP = "\U0001f525", "\U0001f44f"


def _event(user_id: int, text: str, n: int, source: str = "telegram", message_id: int | None = None) -> Event:
    payload = {"text": text}
    if message_id is not None:
        payload["message_id"] = message_id
    return Event(id=f"tg:update:{n}", user_id=user_id, type=EventType.USER_MESSAGE,
                 occurred_at=timeutil.now(), source=source, payload=payload, trust=Trust.USER)


async def test_reaction_marker_is_stripped_and_lands(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text(f"Yes! Offer in hand!\n[react: {FIRE}]")
    await run_turn(_event(user.id, "got the job!!", 1, message_id=500))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Yes! Offer in hand!"]
    assert channel.reactions == [(77, 500, FIRE)]
    assert (await messages.recent(user.id))[-1].content == "Yes! Offer in hand!"
    assert "[react: EMOJI]" in fake_llm.calls[-1][0].content  # the model was offered the choice


async def test_invalid_reaction_dropped_and_seen_cue_cleared(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Ok, noted.\n[react: \U0001f595]")
    await run_turn(_event(user.id, "fine whatever", 1, message_id=501))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Ok, noted."]
    assert channel.reactions == [(77, 501, None)]


async def test_no_reaction_on_consecutive_messages(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    for i, (reply, mid) in enumerate([(f"Nice!\n[react: {FIRE}]", 10), (f"Good.\n[react: {CLAP}]", 11),
                                      ("Sure.", 12), (f"Done!\n[react: {CLAP}]", 13)]):
        fake_llm.push_text(reply)
        await run_turn(_event(user.id, f"msg {i}", i, message_id=mid))
    assert channel.reactions == [(77, 10, FIRE), (77, 11, None), (77, 12, None), (77, 13, CLAP)]


async def test_non_telegram_turn_gets_no_reaction_rule_but_marker_still_stripped(
    db, channel, fake_llm, memory, bus
):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text(f"Hi!\n[react: {FIRE}]")
    await run_turn(_event(user.id, "hi", 1, source="console"))
    await OutboxSender(channel).run_once()
    assert channel.texts == ["Hi!"] and channel.reactions == []
    assert "[react: EMOJI]" not in fake_llm.calls[-1][0].content


async def test_marker_only_reply_falls_back_to_text(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text(f"[react: {FIRE}]")
    await run_turn(_event(user.id, "lol", 1, message_id=9))
    await OutboxSender(channel).run_once()
    assert channel.texts and "[react" not in channel.texts[0]


async def test_landed_reactions_replay_as_markers_and_a_reminder_follows(db, channel, fake_llm, memory, bus):
    from langchain_core.messages import AIMessage, SystemMessage

    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text(f"Huge!\n[react: {FIRE}]")
    await run_turn(_event(user.id, "got the job", 1, message_id=10))
    fake_llm.push_text("Ha, noted.")
    await run_turn(_event(user.id, "and a raise", 2, message_id=11))  # rate-limited turn: no marker
    fake_llm.push_text("Sure.")
    await run_turn(_event(user.id, "ok", 3, message_id=12))
    prompt = fake_llm.calls[-1]
    replies = [m.content for m in prompt if isinstance(m, AIMessage)]
    assert replies[0].endswith(f"Huge!\n[react: {FIRE}]")
    assert not replies[1].endswith("]")
    assert isinstance(prompt[-1], SystemMessage) and "[react: EMOJI]" in prompt[-1].content
    stored = [m.content for m in await messages.recent(user.id) if m.role == "assistant"]
    assert all("[react" not in c for c in stored)  # the store stays marker-free


async def test_no_replay_or_reminder_without_a_reaction_target(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text("Hi")
    await run_turn(_event(user.id, "hi", 1, source="console"))
    assert fake_llm.calls[-1][-1].content == "hi"


async def test_early_returns_settle_the_seen_cue(db, channel, fake_llm, memory, bus, monkeypatch):
    from mavis.agents import commands, conversation

    user, _ = await users.get_or_create_by_chat(77, "Jai")

    async def approval_reply(*a, **k):
        return True

    monkeypatch.setattr(conversation, "_approval_reply", approval_reply)
    await run_turn(_event(user.id, "yes send it", 1, message_id=30))

    async def command(event):
        return True

    monkeypatch.setattr(commands, "run_command", command)
    await run_turn(_event(user.id, "/connections", 2, message_id=31))
    assert channel.reactions == [(77, 30, None), (77, 31, None)]


async def test_retry_does_not_settle_twice(db, channel, fake_llm, memory, bus):
    user, _ = await users.get_or_create_by_chat(77, "Jai")
    fake_llm.push_text(f"Nice!\n[react: {FIRE}]")
    event = _event(user.id, "won the match", 1, message_id=40)
    await run_turn(event)
    await run_turn(event)  # retry after the reply was enqueued
    assert channel.reactions == [(77, 40, FIRE)]
