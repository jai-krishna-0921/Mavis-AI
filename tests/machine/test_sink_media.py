"""Sink rows for the new kinds, fixture downloads, and the owner mirror."""

from __future__ import annotations

import pytest

from mavis.channels import test_sink
from mavis.channels.fake import FakeChannel
from mavis.channels.test_sink import MIRROR_HEADER, SinkChannel, read_sink
from mavis.domain.messages import Button

TEST_CHAT = -1_000_000_000_000_007


@pytest.fixture
def sink(settings):
    inner = FakeChannel()
    return SinkChannel(inner, TEST_CHAT), inner


async def test_test_chat_edits_photos_and_albums_are_recorded_not_sent(sink, settings, tmp_path):
    ch, inner = sink
    await ch.edit_text(TEST_CHAT, -3, "Working on: compare kettles",
                       [[Button(label="Cancel", data="tk:1:x")]])
    await ch.send_photo(TEST_CHAT, str(tmp_path / "s.png"), "Screenshot of kettles.example")
    await ch.send_media_group(TEST_CHAT, ["/a.png", "/b.png"], ["1", "2"])
    rows = read_sink(settings.data_dir)
    assert [r["kind"] for r in rows] == ["edit", "photo", "album"]
    assert rows[0]["message_id"] == -3 and rows[0]["buttons"] == [["Cancel"]]
    assert rows[2]["paths"] == ["/a.png", "/b.png"]
    assert inner.edits == [] and inner.photos == [] and inner.albums == []


@pytest.mark.parametrize("chat", [5, 123456789, -42])
async def test_other_chats_pass_through(sink, chat):
    ch, inner = sink
    await ch.edit_text(chat, 10, "hello")
    await ch.send_photo(chat, "/x.png", "c")
    assert inner.edits == [(chat, 10, "hello", [])] and inner.photos == [(chat, "/x.png", "c")]


@pytest.mark.parametrize("name", ["sales.csv", "notes.txt", "report.xlsx"])
async def test_fixture_ids_resolve_from_the_fixture_dir(sink, tmp_path, monkeypatch, name):
    ch, _ = sink
    (tmp_path / name).write_bytes(b"data:" + name.encode())
    monkeypatch.setattr(test_sink, "FIXTURE_DIR", tmp_path)
    dest = tmp_path / "out" / name
    assert await ch.download_file(f"fixture:{name}", str(dest)) == str(dest)
    assert dest.read_bytes() == b"data:" + name.encode()


@pytest.mark.parametrize("bad", ["fixture:../secrets.env", "fixture:/etc/passwd", "fixture:a/../../b"])
async def test_fixture_ids_cannot_escape(sink, tmp_path, monkeypatch, bad):
    ch, _ = sink
    monkeypatch.setattr(test_sink, "FIXTURE_DIR", tmp_path)
    with pytest.raises(ValueError):
        await ch.download_file(bad, str(tmp_path / "x"))


@pytest.fixture
def mirrored(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[555001]")
    monkeypatch.setenv("TEST_MIRROR_CHAT_ID", "555001")
    get_settings.cache_clear()
    inner = FakeChannel()
    yield SinkChannel(inner, TEST_CHAT), inner
    get_settings.cache_clear()


async def test_mirror_copies_text_without_buttons_and_mirrors_edits(mirrored):
    ch, inner = mirrored
    [sid] = await ch.send_text(TEST_CHAT, "Working on: a table of desks",
                               [[Button(label="Cancel", data="tk:2:x")]])
    await ch.edit_text(TEST_CHAT, sid, "Done: a table of desks")
    assert inner.sent[0].chat_id == 555001 and inner.sent[0].buttons == []
    assert inner.sent[0].text.startswith(MIRROR_HEADER)
    assert inner.edits and inner.edits[0][0] == 555001 and inner.edits[0][2].startswith(MIRROR_HEADER)
    assert inner.edits[0][3] == []


async def test_mirror_requires_the_chat_to_be_allowlisted(settings, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("TEST_MIRROR_CHAT_ID", "777")  # not allowlisted
    get_settings.cache_clear()
    inner = FakeChannel()
    await SinkChannel(inner, TEST_CHAT).send_text(TEST_CHAT, "x")
    assert inner.sent == []
    get_settings.cache_clear()


async def test_fixture_file_id_from_a_real_chat_is_stripped(settings, db, recording_bus, monkeypatch):
    from mavis.channels.telegram_updates import ingest_update

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[3141]")
    from mavis.config import get_settings
    get_settings.cache_clear()
    update = {"update_id": 9001, "message": {"message_id": 1, "date": 1_760_000_000, "chat": {"id": 3141},
              "from": {"first_name": "Ana"},
              "document": {"file_id": "fixture:sales.csv", "file_name": "s.csv"}}}
    assert await ingest_update(update, recording_bus, answer=lambda _id: None)
    [event] = recording_bus.take()
    assert "file" not in event.payload


async def test_reply_to_a_mirror_message_is_ignored(settings, db, recording_bus, monkeypatch):
    from mavis.channels.telegram_updates import ingest_update
    from mavis.config import get_settings

    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[2718]")
    get_settings.cache_clear()
    update = {"update_id": 9002, "message": {"message_id": 2, "date": 1_760_000_000, "chat": {"id": 2718},
              "from": {"first_name": "Bo"}, "text": "nice",
              "reply_to_message": {"from": {"is_bot": True}, "text": "[test] Working on: x"}}}
    assert await ingest_update(update, recording_bus, answer=lambda _id: None) is False
