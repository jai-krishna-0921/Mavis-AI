from __future__ import annotations

import pytest

from mavis.channels.telegram_updates import ingest_update
from mavis.domain.events import EventType
from mavis.store.repo import messages, users


def _msg(update_id: int, chat_id: int, chat_type: str, text: str, uid: int | None = None) -> dict:
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": 1760000000, "text": text,
        "chat": {"id": chat_id, "type": chat_type}, "from": {"id": uid or chat_id, "first_name": "Aiko"}}}


async def _noop(_cid: str) -> None:
    return None


def _take(bus) -> list:
    out, bus.events = list(bus.events), []
    return out


@pytest.mark.parametrize("chat_id,chat_type", [(-4001, "group"), (-1009001, "supergroup"),
                                               (-1009002, "channel")])
async def test_group_and_channel_updates_are_dropped(db, invite_mode, recording_bus, chat_id, chat_type):
    assert not await ingest_update(_msg(1, chat_id, chat_type, "hi all"), recording_bus, _noop)
    assert _take(recording_bus) == [] and await users.get_by_chat(chat_id) is None


@pytest.mark.parametrize("chat,text", [(5001, "what's the weather"), (7302, "remind me at 6"),
                                       (9944, "hello")])
async def test_pending_user_text_is_not_stored(db, invite_mode, recording_bus, chat, text):
    assert await ingest_update(_msg(10 + chat, chat, "private", text), recording_bus, _noop)
    (event,) = _take(recording_bus)
    assert event.payload.get("text", "") == ""  # no content for a pending user
    assert event.payload["pending"] is True
    u = await users.get_by_chat(chat)
    assert u.status == "pending" and await messages.recent(u.id, 10) == []


@pytest.mark.parametrize("text", ["/start MAV7K3QZ9XW2B", "MAV-7K3QZ-9XW2B", "7k3qz 9xw2b"])
async def test_pending_user_code_shaped_text_reaches_the_gate(db, invite_mode, recording_bus, text):
    assert await ingest_update(_msg(77, 6101, "private", text), recording_bus, _noop)
    (event,) = _take(recording_bus)
    assert event.payload["text"] == text


async def test_listed_owner_is_never_treated_as_pending(db, invite_mode, recording_bus, monkeypatch):
    """Grandfathered owner: even a fresh pending row for an owner chat keeps its message text."""
    monkeypatch.setattr(invite_mode, "owner_telegram_chat_ids", [8123])
    assert await ingest_update(_msg(91, 8123, "private", "morning plan please"), recording_bus, _noop)
    (event,) = _take(recording_bus)
    assert event.payload["text"] == "morning plan please" and "pending" not in event.payload


async def test_my_chat_member_becomes_an_event(db, invite_mode, recording_bus):
    data = {"update_id": 900, "my_chat_member": {
        "chat": {"id": -4002, "type": "group"}, "from": {"id": 5001},
        "new_chat_member": {"status": "member", "user": {"id": 1, "is_bot": True}}, "date": 1760000000}}
    assert await ingest_update(data, recording_bus, _noop)
    (event,) = _take(recording_bus)
    assert event.type is EventType.CHAT_MEMBER
    assert event.payload == {"chat_id": -4002, "chat_type": "group", "status": "member"}


async def test_over_the_bucket_publishes_one_rate_limited_event_per_minute(db, invite_mode, recording_bus,
                                                                           monkeypatch):
    from mavis.access import inbound

    class _Never:
        async def allow(self, chat_id, *, pending):
            return False

    monkeypatch.setattr(inbound, "_limiter", _Never())
    for i in range(4):
        await ingest_update(_msg(200 + i, 5001, "private", "spam"), recording_bus, _noop)
    events = _take(recording_bus)
    assert [e.type for e in events] == [EventType.RATE_LIMITED]


async def test_allowlist_mode_is_unchanged(db, settings, recording_bus):
    """Defaults: dev env with an empty owner list still admits every chat, as today."""
    assert await ingest_update(_msg(300, 5555, "private", "hi"), recording_bus, _noop)
    (event,) = _take(recording_bus)
    assert event.payload["text"] == "hi" and "pending" not in event.payload


async def test_allowlist_mode_still_admits_groups_as_before(db, settings, recording_bus):
    assert await ingest_update(_msg(301, -4003, "group", "hi"), recording_bus, _noop)


async def _albumed(bus, chat: int, n: int, base: int) -> int:
    return sum([await ingest_update(_msg(base + i, chat, "private", f"photo {i}"), bus, _noop)
                for i in range(n)])


async def test_active_user_album_of_15_is_not_dropped(db, invite_mode, recording_bus):
    u, _ = await users.get_or_create_by_chat(6501, "Lena")
    await users.update(u.id, status="active")
    assert await _albumed(recording_bus, 6501, 15, 1000) == 15
    assert [e.type for e in _take(recording_bus)] == [EventType.USER_MESSAGE] * 15


async def test_owner_is_never_rate_limited_even_in_a_flood(db, invite_mode, recording_bus, monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", "[6502]")
    from mavis.config import get_settings
    get_settings.cache_clear()
    assert await _albumed(recording_bus, 6502, 300, 2000) == 300
    assert all(e.type is EventType.USER_MESSAGE for e in _take(recording_bus))


async def test_a_flood_from_an_active_user_is_slowed_with_a_notice_not_silently(db, invite_mode,
                                                                                 recording_bus):
    u, _ = await users.get_or_create_by_chat(6503, "Sam")
    await users.update(u.id, status="active")
    await _albumed(recording_bus, 6503, 80, 3000)
    types = [e.type for e in _take(recording_bus)]
    assert types.count(EventType.USER_MESSAGE) == 60 and EventType.RATE_LIMITED in types
