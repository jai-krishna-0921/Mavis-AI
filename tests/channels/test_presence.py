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
