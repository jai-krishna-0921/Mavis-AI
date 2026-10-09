"""One admission rule for the doors that are not the Telegram gate: Slack inbound and OAuth connects."""

from __future__ import annotations

import httpx
import pytest

from mavis.access import admission, gate
from mavis.config import get_settings
from mavis.domain.events import Event, EventType, Trust
from mavis.store.db import utcnow
from mavis.store.repo import users
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import OAuthError
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL, SLACK_TOKEN_URL
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures
from tests.tools.integrations.native.test_oauth import google_vendor, query

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK
OWNER_CHAT = 6900


async def make(chat: int, status: str, tier: str = "standard") -> int:
    u, _ = await users.get_or_create_by_chat(chat, f"user{chat}")
    await users.update(u.id, status=status, tier=tier)
    return u.id


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_IDS", f"[{OWNER_CHAT}]")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("status", ["active", "pending", "banned", "deleting", "deleted"])
@pytest.mark.parametrize("owner", [False, True])
async def test_admission_agrees_with_the_gate_in_invite_mode(db, invite_mode, owner_env, status, owner):
    """The rule is written once in access.admission and once as the gate's flow: they must not drift."""
    uid = await make(OWNER_CHAT if owner else 6901, status, "owner" if owner else "standard")
    ev = Event(id=f"adm:{status}:{owner}", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(),
               source="telegram", payload={"text": "hello there"}, trust=Trust.USER)
    user = await users.get(uid)
    expected = admission.admitted(user)
    # deleting/deleted owners are served by the gate only through grandfathering of the chat id; the shared
    # rule is stricter on purpose (an erased account never connects anything)
    if owner and status in ("deleting", "deleted"):
        assert expected is False
        return
    assert await gate.access_gate(ev) is expected


@pytest.mark.parametrize("mode", ["allowlist", "shadow"])
@pytest.mark.parametrize("status", ["banned", "deleting", "deleted"])
async def test_refused_statuses_stay_refused_in_every_access_mode(db, settings, monkeypatch, mode, status):
    monkeypatch.setenv("ACCESS_MODE", mode)
    get_settings.cache_clear()
    uid = await make(6902, status)
    assert admission.admitted(await users.get(uid)) is False
    get_settings.cache_clear()


async def test_allowlist_mode_does_not_enforce_activation(db, settings):
    uid = await make(6903, "pending")
    assert admission.admitted(await users.get(uid)) is True  # legacy: the allowlist was the gate


async def test_missing_user_is_not_admitted(db, settings):
    assert await admission.admitted_id(None) is False and await admission.admitted_id(987654) is False


# --- Slack inbound -------------------------------------------------------------------------------------


@pytest.fixture
async def slack_world(db, invite_mode, owner_env, client, native_env):
    """Three Mavis users, each with an ACTIVE Slack grant in one workspace, in different statuses."""
    from mavis.tools.integrations.native.tokens import NativeTokenStore

    tokens = NativeTokenStore(client)
    ids = {"active": await make(7001, "active"), "pending": await make(7002, "pending"),
           "banned": await make(7003, "banned")}
    for n, uid in enumerate(ids.values(), start=1):
        # save() itself refuses banned users, so seed the grant while the user is still active
        await users.update(uid, status="active")
        await tokens.save(uid, S, account={"team_id": "T1", "user_id": f"U{n}", "scopes": []},
                          access_token="a", refresh_token=None, expires_at=None)
    await users.update(ids["pending"], status="pending")
    await users.update(ids["banned"], status="banned")
    return tokens, ids


async def test_slack_lookup_maps_only_users_the_gate_would_serve(slack_world):
    tokens, ids = slack_world
    assert await tokens.user_for_slack("T1", "U1") == ids["active"]
    assert await tokens.user_for_slack("T1", "U2") is None  # pending
    assert await tokens.user_for_slack("T1", "U3") is None  # banned
    assert await tokens.user_for_slack("T2", "U1") is None  # same Slack id, other workspace
    assert await tokens.user_for_slack("T1", "U9") is None  # nobody


async def test_slack_dm_from_a_pending_or_banned_user_is_not_processed(slack_world, monkeypatch):
    from mavis.channels import routing, slack_inbound
    from mavis.tools.integrations.native import slack_events
    from tests.channels.slack_fakes import SlackFake, callback, dm_message
    from tests.tools.integrations.fakes import FakeBus

    tokens, ids = slack_world
    slack_inbound._told.clear()
    monkeypatch.setattr(slack_events, "_dedupe", slack_events.EventDedupe())
    routing.set_slack_channel(SlackFake())
    try:
        bus = FakeBus()
        for n, expect in ((1, 1), (2, 0), (3, 0)):
            ev = dm_message("hello", user=f"U{n}", ts=f"17600001{n}0.000100")
            ev["channel_type"] = "im"
            payload = callback(ev, event_id=f"Ev{n}", user_auth=f"U{n}")
            payload["team_id"] = "T1"
            for a in payload["authorizations"]:
                a["team_id"] = "T1"
            counts = await slack_events.handle_callback(payload, tokens, bus)
            assert counts["published"] == expect, (n, counts)
        assert [e.user_id for e in bus.events] == [ids["active"]]
    finally:
        routing.set_slack_channel(None)


async def test_a_queued_slack_turn_of_a_user_banned_since_is_dropped_by_the_gate(db, invite_mode):
    uid = await make(7010, "banned")
    ev = Event(id="sl:1", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="slack_chat",
               payload={"text": "hi"}, trust=Trust.USER)
    assert await gate.access_gate(ev) is False


async def test_slack_text_never_redeems_an_invite_code(db, invite_mode):
    from mavis.store.repo import invites

    uid = await make(7011, "pending")
    _, plain = await invites.mint(created_by=1)
    ev = Event(id="sl:2", user_id=uid, type=EventType.USER_MESSAGE, occurred_at=utcnow(), source="slack_chat",
               payload={"text": plain}, trust=Trust.USER)
    assert await gate.access_gate(ev) is False
    assert (await users.get(uid)).status == "pending"
    tg = ev.model_copy(update={"id": "tg:2", "source": "telegram"})
    assert await gate.access_gate(tg) is False  # the same code on Telegram does activate the account
    assert (await users.get(uid)).status == "active"


# --- OAuth connect -------------------------------------------------------------------------------------


@pytest.fixture
async def connectable(db, invite_mode, owner_env, native_env, client):
    from mavis.tools.integrations.native.oauth import NativeOAuth
    from mavis.tools.integrations.native.tokens import NativeTokenStore

    tokens = NativeTokenStore(client)
    return tokens, NativeOAuth(tokens, client)


@pytest.mark.parametrize("status", ["pending", "banned", "deleting", "deleted"])
async def test_no_consent_link_for_a_user_who_may_not_be_served(connectable, status):
    _, oauth_ = connectable
    uid = await make(7100, status)
    with pytest.raises(OAuthError) as err:
        await oauth_.authorize_url(uid, G)
    assert err.value.kind == "not_allowed"


@pytest.mark.parametrize("status", ["banned", "deleting", "deleted"])
@pytest.mark.parametrize("provider", [G, S])
async def test_a_consent_started_before_the_ban_cannot_complete(connectable, vendor, status, provider):
    tokens, oauth_ = connectable
    uid = await make(7101, "active")
    state = query(await oauth_.authorize_url(uid, provider))["state"]
    await users.update(uid, status=status)
    google_vendor(vendor)
    vendor.routes[SLACK_TOKEN_URL] = lambda r: httpx.Response(200, json={
        "ok": True, "team": {"id": "T1"}, "authed_user": {"id": "U1", "scope": "x", "access_token": "xoxp"}})
    with pytest.raises(OAuthError) as err:
        await oauth_.complete(state, "code", provider)
    assert err.value.kind == "not_allowed" and err.value.user_id is None  # silent: no message to this user
    assert vendor.to(GOOGLE_TOKEN_URL) == [] and vendor.to(SLACK_TOKEN_URL) == []  # the code is never spent
    assert await tokens.grants(uid) == []


async def test_a_ban_that_lands_during_the_token_exchange_still_stores_nothing(connectable, vendor):
    """The status check before the exchange can be stale by the time the grant is saved: save() rechecks
    under the user row lock, which is what deletion relies on."""
    tokens, oauth_ = connectable
    uid = await make(7102, "active")
    state = query(await oauth_.authorize_url(uid, G))["state"]
    google_vendor(vendor)

    real_save = tokens.save

    async def save_after_ban(*a, **kw):
        await users.update(uid, status="banned")
        return await real_save(*a, **kw)

    tokens.save = save_after_ban
    with pytest.raises(OAuthError) as err:
        await oauth_.complete(state, "code", G)
    assert err.value.kind == "not_allowed" and await tokens.grants(uid) == []


async def test_an_active_user_still_connects(connectable, vendor):
    tokens, oauth_ = connectable
    uid = await make(7103, "active")
    google_vendor(vendor)
    state = query(await oauth_.authorize_url(uid, G))["state"]
    done = await oauth_.complete(state, "code", G)
    assert done.user_id == uid and (await tokens.grant(uid, G)).status == "ACTIVE"


async def test_the_callback_route_stays_silent_for_a_banned_user(connectable, vendor):
    from fastapi import FastAPI
    from sqlalchemy import select

    from mavis.api.routes import oauth as oauth_route
    from mavis.bus import get_bus
    from mavis.store import db as dbm
    from mavis.store.models import OutboxMessage
    from mavis.tools.integrations import get_provider
    from mavis.tools.integrations.native.router import NativeRouter
    from tests.tools.integrations.fakes import FakeBus, FakeProvider

    tokens, oauth_ = connectable
    uid = await make(7104, "active")
    state = query(await oauth_.authorize_url(uid, G))["state"]
    await users.update(uid, status="banned")
    app = FastAPI()
    app.include_router(oauth_route.router)
    router = NativeRouter(FakeProvider(), tokens, oauth_, [], None)
    app.dependency_overrides[get_bus] = lambda: FakeBus()
    app.dependency_overrides[get_provider] = lambda: router
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 400 and "could not be connected" in r.text
    async with dbm.Session() as s:
        assert list(await s.scalars(select(OutboxMessage))) == []  # a banned user is told nothing
    assert await tokens.grants(uid) == []
