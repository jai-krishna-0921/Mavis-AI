from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mavis.attention import slack_replies as sr
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.store.repo import users

CHAT = "ms:19:abcdef0123456789abcdef0123456789_fedcba9876543210fedcba9876543210@unq.gbl.spaces"


def _event(uid, *, from_me=False, chat_type="oneOnOne", age_days=0, from_id="u-priya", mid="m1", chat=CHAT):
    when = datetime.fromtimestamp(timeutil.now().timestamp() - age_days * 86400, UTC)
    payload = {
        "chat_id": chat,
        "message_id": mid,
        "from": "Priya Nair",
        "from_id": from_id,
        "from_address": "priya@corp.test",
        "from_me": from_me,
        "text": "can you review the deck?",
        "at": when.isoformat(),
        "chat_type": chat_type,
        "members": [],
    }
    return Event(
        id=f"teams:{uid}:{chat}:{mid}",
        user_id=uid,
        type=EventType.TEAMS_MESSAGE,
        occurred_at=when,
        source="poller",
        payload=payload,
        trust=Trust.UNTRUSTED,
    )


@pytest.fixture
async def setup(db, monkeypatch):
    user, _ = await users.get_or_create_by_chat(71, "JK")
    booked = []

    class Wakeups:
        async def wake_me(self, user_id, at, reason, loop_id=None, kind=None, **kw):
            booked.append({"at": at, "kind": kind, **kw})
            return len(booked)

    monkeypatch.setattr("mavis.timers.service.WakeupService", Wakeups)
    return SimpleNamespace(user=user, booked=booked)


async def _owed(uid):
    return (await users.get_state(uid)).get(sr.OWED_KEY) or {}


async def test_a_1to1_message_opens_one_entry_and_books_one_nudge(setup):
    uid = setup.user.id
    await sr.on_teams_event(_event(uid))
    await sr.on_teams_event(_event(uid, mid="m2"))  # a second message keeps the first one's time
    key = sr.teams_key(CHAT)
    owed = await _owed(uid)
    assert list(owed) == [key] and owed[key]["source"] == "teams" and owed[key]["chat"] == CHAT
    assert owed[key]["from"] == "Priya Nair" and len(key) < 24
    [wake] = setup.booked
    assert (
        wake["kind"].value == "system_slack_reply"
        and wake["payload"]["source"] == "teams"
        and wake["payload"]["channel"] == key
    )
    assert wake["dedupe_key"].startswith("teams_reply:")
    await sr.on_teams_event(_event(uid, from_me=True, mid="m3"))
    assert await _owed(uid) == {}


@pytest.mark.parametrize(
    "change", [{"chat_type": "group"}, {"chat_type": "meeting"}, {"from_id": ""}, {"age_days": 5}]
)
async def test_what_is_not_a_1to1_owed(setup, change):
    await sr.on_teams_event(_event(setup.user.id, **change))
    assert await _owed(setup.user.id) == {} and setup.booked == []


async def test_nudge_text_names_teams_and_the_buttons_fit_telegram(setup, monkeypatch):
    uid = setup.user.id
    await sr.on_teams_event(_event(uid))
    key = sr.teams_key(CHAT)
    sent = {}

    class Exec:
        async def notify(self, user, intent, *, untrusted, buttons):
            sent.update(intent=intent, buttons=buttons)
            return True

    monkeypatch.setattr("mavis.initiative.wiring.current", lambda: SimpleNamespace(executor=Exec()))
    ts = (await _owed(uid))[key]["ts"]
    await sr.on_wakeup(uid, "x", {"channel": key, "ts": ts, "source": "teams"})
    assert "Teams message in a 1:1 chat" in sent["intent"].intent and "Slack" not in sent["intent"].intent
    data = [b.data for row in sent["buttons"] for b in row]
    assert data == [f"sr:draft:{key}", f"sr:skip:{key}"] and all(len(d.encode()) < 64 for d in data)
    await sr.on_wakeup(uid, "x", {"channel": key, "ts": ts, "source": "teams"})  # raised once only
    assert (await _owed(uid))[key]["notified"] is True


async def test_draft_button_starts_a_turn_about_the_chat(setup, monkeypatch):
    uid = setup.user.id
    await sr.on_teams_event(_event(uid))
    key = sr.teams_key(CHAT)
    published = []

    class Bus:
        async def publish(self, event):
            published.append(event)

    monkeypatch.setattr("mavis.bus.get_bus", lambda: Bus())
    btn = Event(
        id="b1",
        user_id=uid,
        type=EventType.BUTTON_PRESSED,
        occurred_at=timeutil.now(),
        source="telegram",
        payload={},
        trust=Trust.USER,
    )
    await sr.on_button(btn, f"sr:draft:{key}")
    [turn] = published
    assert "Teams message Priya Nair sent me" in turn.payload["text"] and CHAT in turn.payload["text"]
    assert "teams_send" in turn.payload["text"] and await _owed(uid) == {}


async def test_catch_up_opens_entries_for_waiting_1to1_chats_only(setup):
    from mavis.domain.integrations import ToolResult

    now = timeutil.now().isoformat()

    class Native:
        async def execute_native(self, provider, user, action, args):
            return ToolResult(
                ok=True,
                data={
                    "chats": [
                        {
                            "id": CHAT,
                            "type": "oneOnOne",
                            "lastMessage": {
                                "id": "m1",
                                "from": "Priya Nair",
                                "fromId": "u1",
                                "fromMe": False,
                                "at": now,
                            },
                        },
                        {
                            "id": "ms:c2",
                            "type": "oneOnOne",
                            "lastMessage": {
                                "id": "m2",
                                "from": "Me",
                                "fromId": "me",
                                "fromMe": True,
                                "at": now,
                            },
                        },
                        {
                            "id": "ms:c3",
                            "type": "group",
                            "lastMessage": {
                                "id": "m3",
                                "from": "X",
                                "fromId": "u3",
                                "fromMe": False,
                                "at": now,
                            },
                        },
                    ]
                },
            )

    assert await sr.catch_up_teams(Native(), setup.user.id) == 1
    assert list(await _owed(setup.user.id)) == [sr.teams_key(CHAT)]


async def test_catch_up_never_raises(setup):
    class Boom:
        async def execute_native(self, *a, **k):
            raise RuntimeError("x")

    assert await sr.catch_up_teams(Boom(), setup.user.id) == 0


async def test_first_sync_entry_point_is_the_same_catch_up():
    from mavis.attention import teams_replies

    assert teams_replies.catch_up is sr.catch_up_teams


async def test_slack_entries_stay_slack(setup):
    await sr.on_teams_event(_event(setup.user.id))
    assert all(k.startswith("teams:") for k in await _owed(setup.user.id))


async def test_an_older_message_arriving_after_the_users_later_reply_opens_nothing(setup):
    uid = setup.user.id
    await sr.on_teams_event(_event(uid, from_me=True, mid="reply"))        # the user answered just now
    await sr.on_teams_event(_event(uid, age_days=0.5, mid="older"))        # the older backlog arrives a tick later
    assert await _owed(uid) == {} and setup.booked == []
    await sr.on_teams_event(_event(uid, age_days=-0.1, mid="newer"))       # a message after the reply does count
    assert list(await _owed(uid)) == [sr.teams_key(CHAT)] and len(setup.booked) == 1
