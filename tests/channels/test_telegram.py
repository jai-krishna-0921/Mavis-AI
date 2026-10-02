from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter

from mavis.channels.base import ChannelRateLimited
from mavis.channels.telegram import TelegramChannel
from mavis.domain.messages import Button


class StubBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.raise_on_send: Exception | None = None
        self._n = 0
        self.fail_html = False
        self.max_len: int | None = None
        self.parse_modes: list[str | None] = []
        self.previews: list[bool | None] = []

    async def initialize(self) -> None:
        self.calls.append(("initialize",))

    async def send_message(
        self, chat_id, text, reply_markup=None, parse_mode=None, link_preview_options=None
    ):
        if self.raise_on_send:
            raise self.raise_on_send
        if self.max_len is not None and len(text) > self.max_len:
            raise BadRequest("Message is too long")
        if self.fail_html and parse_mode == "HTML":
            raise BadRequest("Can't parse entities: unsupported start tag")
        self._n += 1
        self.parse_modes.append(parse_mode)
        self.previews.append(link_preview_options.is_disabled)
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


async def test_sends_html_with_parse_mode_and_no_preview() -> None:
    bot = StubBot()
    await TelegramChannel("token", bot=bot).send_text(5, "**hi** there")
    assert bot.parse_modes == ["HTML"] and bot.previews == [True]
    assert [c for c in bot.calls if c[0] == "message"][0][2] == "<b>hi</b> there"


async def test_bad_request_parse_error_falls_back_to_plain() -> None:
    bot = StubBot()
    bot.fail_html = True
    await TelegramChannel("token", bot=bot).send_text(5, "**hi** - there")
    assert bot.parse_modes == [None]
    assert [c for c in bot.calls if c[0] == "message"][0][2] == "hi - there"


async def test_other_bad_request_is_not_swallowed() -> None:
    bot = StubBot()
    bot.raise_on_send = BadRequest("Chat not found")
    with pytest.raises(BadRequest):
        await TelegramChannel("token", bot=bot).send_text(5, "hi")


async def test_expanded_html_over_limit_is_resplit() -> None:
    bot = StubBot()
    text = "\n".join(["<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"] * 55)  # ~3.6k raw
    ids = await TelegramChannel("token", bot=bot).send_text(5, text[:3500])
    msgs = [c[2] for c in bot.calls if c[0] == "message"]
    assert len(ids) >= 2 and all(len(m) <= 4096 for m in msgs)


async def test_too_long_bad_request_resplits() -> None:
    bot = StubBot()
    bot.max_len = 100
    ids = await TelegramChannel("token", bot=bot).send_text(5, "word " * 60)
    assert len(ids) >= 3
    assert all(len(c[2]) <= 100 for c in bot.calls if c[0] == "message")
