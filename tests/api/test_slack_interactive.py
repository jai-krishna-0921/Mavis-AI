"""POST /webhooks/slack/interactive: signature first, then owner-only button presses and slash commands."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from mavis.api.routes import slack
from mavis.bus import get_bus
from mavis.channels import routing, slack_interactive
from mavis.channels.slack import chat_id, id_to_ts
from mavis.domain.events import EventType
from mavis.tools.integrations.native import slack_events
from tests.channels.slack_fakes import ALICE, ME, MY_DM, STRANGER, TEAM, Lookup, SlackFake
from tests.tools.integrations.fakes import FakeBus

SECRET = "slack-signing-test-secret"
URL = "https://hooks.slack.com/actions/T0/B0/xyz"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(slack, "get_settings", lambda: type("S", (), {"slack_signing_secret": SECRET})())
    slack_events.set_user_lookup(Lookup())
    routing.set_slack_channel(SlackFake())

    async def dm_of_user(user_id):
        return {7: chat_id(TEAM, MY_DM), 8: chat_id(TEAM, "D0ALICE001")}.get(user_id)

    monkeypatch.setattr(routing, "slack_chat_for", dm_of_user)
    posted: list[tuple[str, dict]] = []

    async def poster(url, body):
        posted.append((url, body))

    slack_interactive.set_poster(poster)
    bus = FakeBus()
    app = FastAPI()
    app.include_router(slack.router)
    app.dependency_overrides[get_bus] = lambda: bus
    yield app, bus, posted
    slack_events.set_user_lookup(None)
    routing.set_slack_channel(None)
    slack_interactive.set_poster(None)


def sign(body: bytes, *, ts=None, secret=SECRET) -> dict:
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


async def post(app, form: dict, headers=None):
    body = urlencode(form).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/webhooks/slack/interactive", content=body,
                            headers=sign(body) if headers is None else headers)


def press(*, user=ME, value="ap:42:ok", channel=MY_DM, ts="1760000500.000100", trigger="trig1",
          action_id=None, url=URL, **extra):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Send this email?"}},
              {"type": "actions", "block_id": "mavis_actions_0", "elements": [{"type": "button"}]}]
    payload = {
        "type": "block_actions", "user": {"id": user, "team_id": TEAM}, "team": {"id": TEAM},
        "trigger_id": trigger, "response_url": url, "container": {"message_ts": ts, "channel_id": channel},
        "channel": {"id": channel},
        "message": {"ts": ts, "text": "Send this email?", "blocks": blocks},
        "actions": [{"type": "button", "action_id": action_id or value, "value": value,
                     "block_id": "mavis_actions_0", "text": {"type": "plain_text", "text": "Send"}}],
        **extra,
    }
    return {"payload": json.dumps(payload)}


async def test_owner_press_publishes_the_same_event_a_telegram_tap_makes(env):
    app, bus, posted = env
    r = await post(app, press())
    assert r.status_code == 200 and r.json() == {"published": 1, "duplicate": 0}
    [ev] = bus.events
    assert ev.type is EventType.BUTTON_PRESSED and ev.user_id == 7 and ev.source == "slack_chat"
    assert ev.payload["data"] == "ap:42:ok" and ev.payload["reply_chat"] == chat_id(TEAM, MY_DM)
    assert id_to_ts(ev.payload["message_id"]) == "1760000500.000100"


async def test_pressed_card_is_updated_so_it_cannot_be_pressed_twice(env):
    app, _, posted = env
    await post(app, press())
    [(url, body)] = posted
    assert url == URL and body["replace_original"] is True
    assert not any(b["type"] == "actions" for b in body["blocks"])
    assert body["blocks"][-1]["elements"][0]["text"] == "Selected: Send"
    assert body["blocks"][0]["text"]["text"] == "Send this email?"


async def test_edit_press_keeps_the_card_open(env):
    app, bus, posted = env
    await post(app, press(value="ap:42:edit"))
    assert len(bus.events) == 1 and posted == []


async def test_stranger_without_a_mavis_account_is_refused_privately(env):
    app, bus, posted = env
    r = await post(app, press(user=STRANGER))
    assert r.status_code == 200 and r.json() == {"refused": 1} and bus.events == []
    [(url, body)] = posted
    assert body["response_type"] == "ephemeral" and body["replace_original"] is False
    assert "/connect slack" in body["text"]


async def test_another_mavis_user_cannot_press_the_owners_card(env):
    app, bus, posted = env
    r = await post(app, press(user=ALICE))  # Alice maps to Mavis user 8, but the card sits in user 7's DM
    assert r.json() == {"refused": 1} and bus.events == []
    assert posted[0][1]["text"].startswith("This card isn't yours")


async def test_card_in_a_shared_channel_is_refused_even_for_a_known_user(env):
    app, bus, _ = env
    await post(app, press(channel="C0CHAN001"))
    assert bus.events == []


async def test_repeated_delivery_publishes_once_and_does_not_re_update(env):
    app, bus, posted = env
    await post(app, press())
    r = await post(app, press())
    assert r.json() == {"published": 0, "duplicate": 1} and len(bus.events) == 1 and len(posted) == 1


async def test_link_buttons_and_empty_values_are_ignored(env):
    app, bus, _ = env
    await post(app, press(value="", action_id="url:0:2"))
    await post(app, press(value="https://x.test", action_id="url:0:2", trigger="t2"))
    assert bus.events == []


async def test_response_url_must_be_slacks(env):
    app, bus, posted = env
    await post(app, press(url="https://evil.example/hook"))
    assert len(bus.events) == 1 and posted == []


@pytest.mark.parametrize("kind", ["view_submission", "shortcut", "block_suggestion"])
async def test_other_interaction_types_are_ignored(env, kind):
    app, bus, _ = env
    r = await post(app, {"payload": json.dumps({"type": kind})})
    assert r.status_code == 200 and bus.events == []


async def test_bad_stale_missing_and_wrong_secret_signatures_are_401(env):
    app, bus, posted = env
    form = press()
    body = urlencode(form).encode()
    good = sign(body)
    cases = [
        {**good, "X-Slack-Signature": "v0=" + "0" * 64},
        sign(body, secret="another-secret"),
        sign(body, ts=int(time.time()) - 3600),
        {"Content-Type": good["Content-Type"]},
        {**good, "X-Slack-Request-Timestamp": "soon"},
    ]
    for headers in cases:
        assert (await post(app, form, headers)).status_code == 401
    tampered = {**good}
    other = urlencode(press(value="ap:99:ok")).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhooks/slack/interactive", content=other, headers=tampered)
    assert r.status_code == 401 and bus.events == [] and posted == []


async def test_unsigned_when_secret_unset_is_refused(env, monkeypatch):
    app, bus, _ = env
    monkeypatch.setattr(slack, "get_settings", lambda: type("S", (), {"slack_signing_secret": ""})())
    assert (await post(app, press())).status_code == 401 and bus.events == []


async def test_junk_bodies_are_400_after_a_valid_signature(env):
    app, _, _ = env
    assert (await post(app, {"payload": "{not json"})).status_code == 400
    assert (await post(app, {"payload": json.dumps([1])})).status_code == 400
    assert (await post(app, {"hello": "world"})).status_code == 400


async def test_slash_command_from_a_known_user_runs_in_the_dm(env):
    app, bus, _ = env
    form = {"command": "/channel", "text": "slack", "user_id": ME, "team_id": TEAM, "trigger_id": "tr9",
            "response_url": URL}
    r = await post(app, form)
    assert r.json()["response_type"] == "ephemeral"
    [ev] = bus.events
    assert ev.type is EventType.USER_MESSAGE and ev.user_id == 7 and ev.payload["text"] == "/channel slack"
    assert ev.payload["command"] == "channel" and ev.payload["reply_chat"] == chat_id(TEAM, MY_DM)


async def test_slash_command_from_a_stranger_or_unknown_command_runs_nothing(env):
    app, bus, _ = env
    r = await post(app, {"command": "/channel", "text": "both", "user_id": STRANGER, "team_id": TEAM})
    assert "/connect slack" in r.json()["text"]
    r = await post(app, {"command": "/rm", "text": "", "user_id": ME, "team_id": TEAM})
    assert bus.events == [] and r.json()["response_type"] == "ephemeral"
