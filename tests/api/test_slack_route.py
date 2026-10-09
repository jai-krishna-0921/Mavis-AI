"""POST /webhooks/slack: signature first, then url_verification, filtering, mapping, replay."""

import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from mavis.api.routes import slack
from mavis.bus import get_bus
from mavis.domain.events import EventType
from mavis.tools.integrations.native import slack_events
from mavis.tools.integrations.normalize import normalize_slack
from tests.tools.integrations.fakes import FakeBus

SECRET = "slack-signing-test-secret"
TEAM, ME, OTHER = "T0TEAM001", "U0ME000001", "U0ALICE001"


class Lookup:
    def __init__(self, mapping):
        self.mapping, self.asked = mapping, []

    async def user_for_slack(self, team_id, slack_user_id):
        self.asked.append((team_id, slack_user_id))
        return self.mapping.get((team_id, slack_user_id))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(slack, "get_settings", lambda: type("S", (), {"slack_signing_secret": SECRET})())
    monkeypatch.setattr(slack_events, "_dedupe", slack_events.EventDedupe())
    lookup = Lookup({(TEAM, ME): 7})
    slack_events.set_user_lookup(lookup)
    bus = FakeBus()
    app = FastAPI()
    app.include_router(slack.router)
    app.dependency_overrides[get_bus] = lambda: bus
    yield app, bus, lookup
    slack_events.set_user_lookup(None)


def sign(body: bytes, *, ts=None, secret=SECRET) -> dict:
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


async def post(app, payload, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        sent = sign(body) if headers is None else headers
        return await c.post("/webhooks/slack", content=body, headers=sent)


def callback(event, *, event_id="Ev0001", auth=ME, extra_auth=()):
    return {
        "token": "legacy", "team_id": TEAM, "api_app_id": "A0APP00001", "type": "event_callback",
        "event_id": event_id, "event_time": 1760000000,
        "authorizations": [{"enterprise_id": None, "team_id": TEAM, "user_id": u, "is_bot": False,
                            "is_enterprise_install": False} for u in (auth, *extra_auth)],
        "event": event,
    }


def message(**kw):
    base = {"type": "message", "channel": "C0000GEN01", "channel_type": "channel", "user": OTHER,
            "text": "can you review the deck?", "ts": "1760000100.000100", "event_ts": "1760000100.000100"}
    return {**base, **kw}


async def test_url_verification_echoes_challenge(env):
    app, bus, _ = env
    challenge = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"
    r = await post(app, {"type": "url_verification", "challenge": challenge, "token": "x"})
    assert r.status_code == 200 and r.json() == {"challenge": challenge}


async def test_message_publishes_event_that_parses_through_normalize(env):
    app, bus, lookup = env
    r = await post(app, callback(message(thread_ts="1760000000.000050")))
    assert r.status_code == 200 and r.json()["published"] == 1
    [event] = bus.events
    assert event.type is EventType.SLACK_MESSAGE and event.user_id == 7 and event.source == "slack"
    assert event.id == "slack:7:C0000GEN01:1760000100.000100"
    assert event.occurred_at.timestamp() == pytest.approx(1760000100.0001)
    p = event.payload
    assert p["text"] == "can you review the deck?" and p["thread_ts"] == "1760000000.000050"
    assert p["from"] == OTHER and p["from_me"] is False and p["channel_type"] == "channel"
    parsed = normalize_slack(p)
    assert (parsed["channel"], parsed["ts"], parsed["user"]) == ("C0000GEN01", "1760000100.000100", OTHER)
    assert lookup.asked == [(TEAM, ME)]


async def test_own_message_is_kept_and_marked(env):
    app, bus, _ = env
    await post(app, callback(message(user=ME, channel="D0000DM001", channel_type="im")))
    [event] = bus.events
    assert event.payload["from_me"] is True and event.payload["channel_type"] == "im"


async def test_replayed_event_id_publishes_once(env):
    app, bus, _ = env
    body = callback(message())
    first = await post(app, body)
    again = await post(app, body)  # Slack retry
    assert first.json()["published"] == 1 and again.json()["duplicate"] == 1 and len(bus.events) == 1


async def test_new_event_id_same_message_deduped_by_bus(env):
    app, bus, _ = env
    await post(app, callback(message(), event_id="Ev0001"))
    r = await post(app, callback(message(), event_id="Ev0002"))
    assert r.json()["duplicate"] == 1 and len(bus.events) == 1


@pytest.mark.parametrize("extra", [
    {"bot_id": "B0BOT00001"},
    {"subtype": "bot_message", "bot_id": "B0BOT00001"},
    {"subtype": "channel_join"}, {"subtype": "channel_leave"}, {"subtype": "channel_topic"},
    {"subtype": "channel_purpose"}, {"subtype": "pinned_item"}, {"subtype": "reminder_add"},
    {"subtype": "some_future_subtype"},
    {"text": "   "}, {"hidden": True},
])
async def test_non_conversation_messages_are_skipped(env, extra):
    app, bus, _ = env
    r = await post(app, callback(message(**extra)))
    assert r.status_code == 200 and r.json()["skipped"] == 1 and bus.events == []


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share"])
async def test_allowed_subtypes_are_kept(env, subtype):
    app, bus, _ = env
    await post(app, callback(message(subtype=subtype)))
    assert len(bus.events) == 1


@pytest.mark.parametrize("subtype", ["message_changed", "message_deleted"])
async def test_edits_and_deletes_are_ignored(env, subtype):
    app, bus, _ = env
    ev = {"type": "message", "subtype": subtype, "channel": "C0000GEN01", "ts": "1760000300.000300",
          "message": {"type": "message", "user": OTHER, "text": "edited", "ts": "1760000100.000100"}}
    r = await post(app, callback(ev))
    assert r.status_code == 200 and bus.events == []


async def test_non_message_events_and_unmapped_users_are_acknowledged(env):
    app, bus, _ = env
    r = await post(app, callback({"type": "reaction_added", "user": OTHER, "reaction": "eyes"}))
    assert r.status_code == 200 and bus.events == []
    r = await post(app, callback(message(), auth="U0STRANGER1", event_id="Ev0009"))
    assert r.json()["unmapped"] == 1 and bus.events == []


async def test_event_delivered_for_two_authorizing_users(env):
    app, bus, lookup = env
    lookup.mapping[(TEAM, "U0SECOND01")] = 8
    await post(app, callback(message(user="U0SECOND01"), extra_auth=("U0SECOND01",)))
    assert {(e.user_id, e.payload["from_me"]) for e in bus.events} == {(7, False), (8, True)}


async def test_no_lookup_wired_acknowledges_without_publishing(env):
    app, bus, _ = env
    slack_events.set_user_lookup(None)
    r = await post(app, callback(message()))
    assert r.status_code == 200 and bus.events == []


async def test_bad_signature_is_rejected_before_parsing(env):
    app, bus, _ = env
    body = b"{not json at all"
    r = await post(app, None, headers=sign(body, secret="wrong"), raw=body)
    assert r.status_code == 401 and bus.events == []
    good = json.dumps(callback(message())).encode()
    tampered = good.replace(b"deck", b"dock")
    r = await post(app, None, headers=sign(good), raw=tampered)
    assert r.status_code == 401 and bus.events == []


async def test_missing_headers_and_stale_timestamp_rejected(env):
    app, bus, _ = env
    body = json.dumps(callback(message())).encode()
    assert (await post(app, None, headers={}, raw=body)).status_code == 401
    stale = sign(body, ts=int(time.time()) - 301)
    assert (await post(app, None, headers=stale, raw=body)).status_code == 401
    fresh_edge = sign(body, ts=int(time.time()) - 200)
    assert (await post(app, None, headers=fresh_edge, raw=body)).status_code == 200
    junk_ts = sign(body)
    junk_ts["X-Slack-Request-Timestamp"] = "yesterday"
    assert (await post(app, None, headers=junk_ts, raw=body)).status_code == 401


async def test_unset_secret_refuses_everything(env, monkeypatch):
    app, bus, _ = env
    monkeypatch.setattr(slack, "get_settings", lambda: type("S", (), {})())
    body = json.dumps(callback(message())).encode()
    assert (await post(app, None, headers=sign(body, secret=""), raw=body)).status_code == 401


async def test_signed_garbage_is_400_and_oversized_is_413(env):
    app, bus, _ = env
    body = b"[1, 2]"
    assert (await post(app, None, headers=sign(body), raw=body)).status_code == 400
    body = b"not json"
    assert (await post(app, None, headers=sign(body), raw=body)).status_code == 400
    big = b"x" * (slack_events.MAX_BODY_BYTES + 1)
    assert (await post(app, None, headers=sign(big), raw=big)).status_code == 413
