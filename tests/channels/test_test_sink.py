"""H7: a configured test chat never reaches Telegram; its sends go to a log sink."""

import json

import pytest

from mavis.channels import get_channel, set_channel
from mavis.channels.fake import FakeChannel
from mavis.channels.telegram_updates import _chat_allowed
from mavis.channels.test_sink import SinkChannel, read_sink
from mavis.domain.messages import Button

TEST_CHAT = 7_000_001
OWNER = 111


@pytest.fixture
def test_chat(settings, monkeypatch):
    monkeypatch.setattr(settings, "test_telegram_chat_id", TEST_CHAT)
    monkeypatch.setattr(settings, "allowed_telegram_chat_ids", [OWNER])
    return settings


async def test_test_chat_sends_go_to_the_sink_and_others_pass_through(test_chat):
    inner = FakeChannel()
    ch = SinkChannel(inner, TEST_CHAT)
    ids = await ch.send_text(TEST_CHAT, "hello test", [[Button(label="OK", data="x")]])
    await ch.send_document(TEST_CHAT, "/tmp/report.pdf", "your report")
    await ch.send_typing(TEST_CHAT)
    await ch.react(TEST_CHAT, 5, "\N{EYES}")
    assert ids and inner.sent == [] and inner.reactions == []
    await ch.send_text(OWNER, "for the owner")
    assert inner.texts == ["for the owner"]
    rows = read_sink(test_chat.data_dir)
    assert [r["kind"] for r in rows] == ["text", "document"]
    assert rows[0]["text"] == "hello test" and rows[0]["buttons"] == [["OK"]]
    assert all(r["chat_id"] == TEST_CHAT for r in rows)
    json.dumps(rows)


async def test_process_channel_is_wrapped_only_when_configured(test_chat, monkeypatch):
    set_channel(None)
    try:
        assert isinstance(get_channel(), SinkChannel)
        set_channel(None)
        monkeypatch.setattr(test_chat, "test_telegram_chat_id", None)
        assert not isinstance(get_channel(), SinkChannel)
    finally:
        set_channel(None)


def test_test_chat_is_admitted_beside_the_allowlist(test_chat, monkeypatch):
    assert _chat_allowed(OWNER) and _chat_allowed(TEST_CHAT)
    assert not _chat_allowed(42)
    monkeypatch.setattr(test_chat, "test_telegram_chat_id", None)
    assert not _chat_allowed(TEST_CHAT)


def test_live_harness_refuses_the_owner_chat(test_chat, monkeypatch):
    from scripts.live_e2e import HarnessError, target_chat

    assert target_chat(test_chat) == TEST_CHAT
    monkeypatch.setattr(test_chat, "test_telegram_chat_id", OWNER)  # pointing at a real chat
    with pytest.raises(HarnessError):
        target_chat(test_chat)
    monkeypatch.setattr(test_chat, "test_telegram_chat_id", None)
    with pytest.raises(HarnessError):
        target_chat(test_chat)
