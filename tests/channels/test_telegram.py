from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from mavis.channels.base import ChannelRateLimited
from mavis.channels.telegram import TelegramChannel
from mavis.domain.messages import Button


class StubBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.raise_on_send: Exception | None = None
        self._n = 0

    async def initialize(self) -> None:
        self.calls.append(("initialize",))

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.raise_on_send:
            raise self.raise_on_send
        self._n += 1
        self.calls.append(("message", chat_id, text, reply_markup))
        return SimpleNamespace(message_id=self._n)

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("action", chat_id, action))

    async def send_document(self, chat_id, document, filename=None, caption=None):
        self.calls.append(("document", chat_id, filename, caption))
        return SimpleNamespace(message_id=99)


async def test_long_text_split_with_buttons_on_last_chunk() -> None:
    bot = StubBot()
    ch = TelegramChannel("token", bot=bot)
    ids = await ch.send_text(5, ("a" * 3000) + "\n" + ("b" * 3000), buttons=[[Button(label="OK", data="ok")]])
    messages = [c for c in bot.calls if c[0] == "message"]
    assert ids == [1, 2]
    assert messages[0][3] is None
    assert messages[1][3].inline_keyboard[0][0].callback_data == "ok"
    assert bot.calls[0] == ("initialize",)


async def test_retry_after_maps_to_channel_rate_limited() -> None:
    bot = StubBot()
    bot.raise_on_send = RetryAfter(5)
    with pytest.raises(ChannelRateLimited) as info:
        await TelegramChannel("token", bot=bot).send_text(5, "hi")
    assert info.value.retry_after == 5.0


async def test_send_document_and_typing(tmp_path) -> None:
    bot = StubBot()
    ch = TelegramChannel("token", bot=bot)
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"x")
    assert await ch.send_document(5, str(f), caption="Your deck") == 99
    await ch.send_typing(5)
    assert ("document", 5, "deck.pptx", "Your deck") in bot.calls
    assert any(c[0] == "action" for c in bot.calls)


def test_get_channel_defaults_to_console_without_token(settings) -> None:
    from mavis.channels import get_channel, set_channel
    from mavis.channels.fake import ConsoleChannel

    set_channel(None)
    try:
        assert isinstance(get_channel(), ConsoleChannel)
    finally:
        set_channel(None)
