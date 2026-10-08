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
