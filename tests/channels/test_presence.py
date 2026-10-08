import asyncio

from mavis.channels import presence, set_channel
from mavis.channels.fake import FakeChannel
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.worker import runner


class BoomChannel(FakeChannel):
    async def react(self, chat_id, message_id, emoji):
        raise RuntimeError("telegram down")

    async def send_typing(self, chat_id):
        raise RuntimeError("telegram down")


async def test_react_records_on_channel(channel):
    await presence.react(7, 3)
    assert channel.reactions == [(7, 3, "\N{EYES}")]


async def test_react_failure_is_swallowed():
    set_channel(BoomChannel())
    try:
        await presence.react(7, 3)  # must not raise
        async with presence.typing(7, interval_s=0.01):
            await asyncio.sleep(0.03)
    finally:
        set_channel(None)


async def test_react_none_message_id_is_noop(channel):
    await presence.react(7, None)
    assert channel.reactions == []


async def test_typing_refreshes_until_exit(channel):
    async with presence.typing(7, interval_s=0.01):
        await asyncio.sleep(0.05)
    count = len([s for s in channel.sent if s.kind == "typing"])
    assert count >= 3
    await asyncio.sleep(0.03)
    assert len([s for s in channel.sent if s.kind == "typing"]) == count  # stopped


async def test_typing_without_chat_id_is_noop(channel):
    async with presence.typing(None):
        pass
    assert channel.sent == []


async def test_worker_reacts_to_telegram_message_before_handlers(user, channel):

    event = Event(id="tg:update:1", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                  source="telegram", payload={"text": "hi", "message_id": 77}, trust=Trust.USER)
    await runner._acknowledge(event)
    assert channel.reactions == [(111, 77, "\N{EYES}")]


async def test_worker_skips_reaction_for_cli_events(user, channel):
    event = Event(id="cli:1", user_id=user.id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                  source="cli", payload={"text": "hi"}, trust=Trust.USER)
    await runner._acknowledge(event)
    assert channel.reactions == []


def test_reaction_is_off_by_default() -> None:
    from mavis.config import Settings

    assert Settings.model_fields["presence_reaction"].default == ""


async def test_no_reaction_when_setting_empty(settings, channel, monkeypatch) -> None:
    from mavis.config import get_settings

    monkeypatch.setenv("PRESENCE_REACTION", "")
    get_settings.cache_clear()
    await presence.react(7, 3)
    assert channel.reactions == []
    async with presence.typing(7, interval_s=0.01):  # typing indicator is unaffected
        await asyncio.sleep(0.03)
    assert any(s.kind == "typing" for s in channel.sent)


class RejectingChannel(FakeChannel):
    """Telegram rejects a reaction it does not allow (REACTION_INVALID); clearing still works."""

    async def react(self, chat_id, message_id, emoji):
        if emoji is not None and emoji != "\N{EYES}":
            raise RuntimeError("Bad Request: REACTION_INVALID")
        await super().react(chat_id, message_id, emoji)


async def test_react_reports_success_and_clear_sends_none(channel):
    assert await presence.react(7, 3, "\U0001f525") is True
    assert await presence.clear(7, 3) is True
    assert channel.reactions == [(7, 3, "\U0001f525"), (7, 3, None)]
    assert await presence.clear(7, None) is False


async def test_mood_reaction_replaces_seen_cue(user, channel):
    from mavis.agents import reactions


    assert await reactions.apply(user.id, 111, 5, "\U0001f525", "e5") == "\U0001f525"
    assert channel.reactions == [(111, 5, "\U0001f525")]  # replaces the seen cue, no separate clear


async def test_no_mood_clears_seen_cue(user, channel):
    from mavis.agents import reactions


    assert await reactions.apply(user.id, 111, 5, None, "e5") == ""
    assert channel.reactions == [(111, 5, None)]


async def test_no_seen_cue_configured_means_nothing_to_clear(user, channel, monkeypatch):
    from mavis.agents import reactions
    from mavis.config import get_settings

    monkeypatch.setenv("PRESENCE_REACTION", "")
    get_settings.cache_clear()

    assert await reactions.apply(user.id, 111, 5, None, "e5") == ""
    assert channel.reactions == []


async def test_rejected_mood_falls_back_to_clearing(user):
    from mavis.agents import reactions

    ch = RejectingChannel()
    set_channel(ch)
    try:

        assert await reactions.apply(user.id, 111, 5, "\U0001f525", "e5") == ""
        assert ch.reactions == [(111, 5, None)]
        # nothing landed, so the next message may still get one
        assert await reactions._log.recent(user.id) == [""]
        # a retry of the same message does not settle twice
        assert await reactions.apply(user.id, 111, 5, "\U0001f525", "e5") == ""
        assert ch.reactions == [(111, 5, None)]
    finally:
        set_channel(None)


async def test_rate_limit_blocks_consecutive_reactions(user, channel):
    from mavis.agents import reactions


    assert await reactions.apply(user.id, 111, 5, "\U0001f525", "e5") == "\U0001f525"
    assert await reactions.apply(user.id, 111, 6, "\U0001f44f", "e6") == ""
    assert await reactions.apply(user.id, 111, 7, "\U0001f44f", "e7") == ""
    assert await reactions.apply(user.id, 111, 8, "\U0001f44f", "e8") == "\U0001f44f"
    assert channel.reactions[1:3] == [(111, 6, None), (111, 7, None)]


async def test_mood_waits_for_a_slow_seen_cue(user, channel):
    from mavis.agents import reactions

    async def slow_ack():
        await asyncio.sleep(0.05)
        await presence.react(111, 5)

    task = asyncio.create_task(slow_ack())
    presence.track_ack("ev1", task)
    assert await reactions.apply(user.id, 111, 5, "\U0001f525", "ev1") == "\U0001f525"
    assert channel.reactions == [(111, 5, "\N{EYES}"), (111, 5, "\U0001f525")]  # the mood lands last
    assert "ev1" not in presence._acks
