"""Edit, photo and album on the fake and the Telegram adapter (PTB Bot mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter

from mavis.channels.base import ChannelRateLimited, MessageGone
from mavis.channels.fake import FakeChannel
from mavis.channels.telegram import TelegramChannel
from mavis.domain.messages import Button


async def test_fake_records_edits_photos_and_albums(tmp_path):
    ch = FakeChannel()
    await ch.edit_text(11, 5, "step 2 of 3", [[Button(label="Cancel", data="tk:9:x")]])
    pid = await ch.send_photo(11, str(tmp_path / "a.png"), "Screenshot of example.org")
    ids = await ch.send_media_group(12, [str(tmp_path / "b.png"), str(tmp_path / "c.png")], ["one", "two"])
    assert ch.edits == [(11, 5, "step 2 of 3", [[Button(label="Cancel", data="tk:9:x")]])]
    assert ch.photos == [(11, str(tmp_path / "a.png"), "Screenshot of example.org")]
    assert ch.albums == [(12, [str(tmp_path / "b.png"), str(tmp_path / "c.png")], ["one", "two"])]
    assert isinstance(pid, int) and len(ids) == 2


async def test_fake_edit_of_a_gone_message_raises():
    ch = FakeChannel()
    ch.gone.add(77)
    with pytest.raises(MessageGone):
        await ch.edit_text(3, 77, "x")


class _Bot:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc, self.calls = exc, []

    async def initialize(self):
        return None

    async def edit_message_text(self, **kw):
        self.calls.append(("edit", kw))
        if self.exc:
            raise self.exc
        return SimpleNamespace(message_id=kw["message_id"])

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        if self.exc:
            raise self.exc
        return SimpleNamespace(message_id=901)

    async def send_media_group(self, **kw):
        self.calls.append(("album", kw))
        return [SimpleNamespace(message_id=902 + i) for i, _ in enumerate(kw["media"])]


async def test_telegram_edit_sends_html_and_markup():
    bot = _Bot()
    ch = TelegramChannel("t", bot=bot)
    await ch.edit_text(40, 8, "**Working on:** plan", [[Button(label="Cancel", data="tk:3:x")]])
    kind, kw = bot.calls[0]
    assert kind == "edit" and kw["chat_id"] == 40 and kw["message_id"] == 8
    assert kw["parse_mode"] == "HTML" and "<b>" in kw["text"]
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "tk:3:x"


async def test_telegram_edit_without_buttons_removes_the_keyboard():
    bot = _Bot()
    await TelegramChannel("t", bot=bot).edit_text(40, 8, "Done")
    assert bot.calls[0][1]["reply_markup"] is None


@pytest.mark.parametrize("message", ["Bad Request: message is not modified: specified new message content",
                                     "Message is not modified"])
async def test_not_modified_is_success(message):
    await TelegramChannel("t", bot=_Bot(BadRequest(message))).edit_text(1, 2, "same")


@pytest.mark.parametrize("message", ["Message to edit not found", "message can't be edited"])
async def test_missing_message_raises_gone(message):
    with pytest.raises(MessageGone):
        await TelegramChannel("t", bot=_Bot(BadRequest(message))).edit_text(1, 2, "x")


async def test_edit_retry_after_maps_to_rate_limited():
    with pytest.raises(ChannelRateLimited) as info:
        await TelegramChannel("t", bot=_Bot(RetryAfter(4))).edit_text(1, 2, "x")
    assert info.value.retry_after == 4


async def test_telegram_photo_and_album(tmp_path):
    paths = []
    for name in ("p1.png", "p2.png", "p3.png"):
        (tmp_path / name).write_bytes(b"\x89PNG")
        paths.append(str(tmp_path / name))
    bot = _Bot()
    ch = TelegramChannel("t", bot=bot)
    assert await ch.send_photo(5, paths[0], "Screenshot of shop.example") == 901
    ids = await ch.send_media_group(5, paths, ["a", "b", "c"])
    assert ids == [902, 903, 904]
    album = bot.calls[1][1]["media"]
    assert [m.caption for m in album] == ["a", "b", "c"]
