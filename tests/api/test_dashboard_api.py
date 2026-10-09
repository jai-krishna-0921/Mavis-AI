# ruff: noqa: E501
"""Dashboard API endpoints and the isolation of one user's session from another's data."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from mavis.domain import timeutil
from mavis.domain.events import JobKind
from mavis.domain.memory import Entity, Relation
from mavis.memory.graph import self_authored_ref, third_party_ref
from mavis.store import db as dbm
from mavis.store.models import InviteCode
from mavis.store.repo import invites, users
from mavis.store.repo import profile as profile_repo
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import GOOGLE_USERINFO_URL
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL
from mavis.web import emails, sessions
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import new_client, query, signed_in

API = "/api/v1"
G = NativeProvider.GOOGLE


async def learn(memory, uid: int, rel: str, obj: str, statement: str, ref: str = "", label: str = "Topic"):
    await memory.init()
    await memory.graph.upsert_entity(uid, Entity(name=obj, label=label))
    await memory.graph.upsert_relation(
        uid, Relation(subject="User", rel=rel, object=obj, statement=statement, confidence=1.0), source_ref=ref)


@pytest.fixture
async def data(memory):
    """Distinctive facts for users 1 (A) and 2 (B)."""
    await learn(memory, 1, "PREFERS", "Tea", "Prefers zebra-quartz tea", label="Topic")
    await learn(memory, 1, "WORKS_AT", "Acme", "Works at Acme Corp", ref=self_authored_ref("gmail:m1"),
                label="Organization")
    await learn(memory, 1, "KNOWS", "Rana", "Rana runs the zebra account", ref=third_party_ref("gmail:m2"))
    await learn(memory, 2, "PREFERS", "Coffee", "Prefers otter-lilac coffee")
    await profile_repo.save(1, (await profile_repo.get(1)).model_copy(update={"goals": ["Ship the zebra launch"]}))


async def items(c, kind):
    r = await c.get(f"{API}/vault/items", params={"kind": kind})
    assert r.status_code == 200, r.text
    return r.json()


# --- /me and preferences ----------------------------------------------------------------------------------


async def test_preferences_round_trip_and_are_honoured(app):
    from mavis.access import web_prefs

    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.patch(f"{API}/preferences", headers=csrf, json={
            "name": "Priya", "timezone": "Europe/Lisbon", "quiet_hours": {"start": "21:30", "end": "06:15"},
            "morning_checkin_time": "07:45", "language_register_opt_out": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["timezone"] == "Europe/Lisbon" and body["quiet_hours"] == {"start": "21:00", "end": "06:00"}
        assert body["morning_checkin_time"] == "07:45" and body["language_register_opt_out"] is True
        assert (await c.get(f"{API}/preferences")).json() == body
        assert (await c.get(f"{API}/me")).json()["name"] == "Priya"
    user = await users.get(1)
    assert web_prefs.quiet_hours(user) == (21, 6) and web_prefs.register_opt_out(user)


@pytest.mark.parametrize("patch", [{"timezone": "Mars/Base"}, {"morning_checkin_time": "25:00"},
                                   {"quiet_hours": {"start": "x", "end": "07:00"}},
                                   {"proactive_channel": "pigeon"}])
async def test_bad_preference_values_are_plain_400s(app, patch):
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.patch(f"{API}/preferences", headers=csrf, json=patch)
        assert r.status_code == 400 and set(r.json()) == {"error", "message"}


async def test_proactive_channel_follows_the_slack_chat_setup(app, monkeypatch):
    from mavis.channels import routing

    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.patch(f"{API}/preferences", headers=csrf, json={"proactive_channel": "both"})
        assert r.status_code == 400 and r.json()["error"] == "slack_not_ready"

        async def ready(uid):
            return "slack:T1:D1"

        monkeypatch.setattr(routing, "slack_chat_for", ready)
        r = await c.patch(f"{API}/preferences", headers=csrf, json={"proactive_channel": "both"})
        assert r.json()["proactive_channel"] == "both"
        assert routing.pref_of((await users.get(1)).state) == "both"


async def test_quiet_hours_and_checkin_time_change_behaviour(app):
    from datetime import UTC, datetime

    from mavis.access import web_prefs
    from mavis.policy.pings import PingPolicy

    await users.update(1, timezone="UTC")
    await web_prefs.update(1, {"quiet_hours": {"start": "10:00", "end": "11:00"}})
    verdict = await PingPolicy().check(await users.get(1), 1, "k1", datetime(2026, 9, 29, 10, 30, tzinfo=UTC))
    assert verdict.allow is False and verdict.reason == "quiet hours"


# --- connectors -------------------------------------------------------------------------------------------


async def test_connectors_report_status_and_service_scopes(app, tokens):
    c, _ = await signed_in(app, 1)
    async with c:
        google, slack = (await c.get(f"{API}/connectors")).json()
        assert (google["id"], google["status"], slack["status"]) == ("google", "none", "none")
        await tokens.save(1, G, account={"email": "a@x.com", "scopes": [
            "openid", "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.events"]}, access_token="TOKEN-A", refresh_token="R", expires_at=None)
        r = await c.get(f"{API}/connectors")
        google = r.json()[0]
        assert google["status"] == "active" and google["account"] == "a@x.com"
        assert google["scopes_granted"] == ["mail", "calendar"]
        assert set(google["missing_scopes"]) == {"drive", "docs", "sheets", "tasks", "meet", "contacts"}
        assert "TOKEN-A" not in r.text and "refresh" not in r.text


async def test_connect_returns_the_native_url_and_the_callback_returns_to_the_workspace(app, oauth, vendor):
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.post(f"{API}/connectors/google/connect", headers=csrf, json={})
        url = r.json()["url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert (await c.post(f"{API}/connectors/nope/connect", headers=csrf, json={})).status_code == 404
        vendor.routes[GOOGLE_TOKEN_URL] = lambda req: httpx.Response(200, json={
            "access_token": "ya29", "refresh_token": "1//r", "expires_in": 3600,
            "scope": "openid email https://www.googleapis.com/auth/gmail.readonly"})
        vendor.routes[GOOGLE_USERINFO_URL] = lambda req: httpx.Response(200, json={"sub": "9", "email": "Me@Kripya.com"})
        cb = await c.get("/oauth/google/callback", params={"code": "c", "state": query(url)["state"]})
        assert cb.status_code == 303 and cb.headers["location"] == "/workspace?connected=google"
    assert await emails.user_for("me@kripya.com") is None  # a grant never becomes a sign-in identity


async def test_a_failed_web_connect_also_returns_to_the_workspace(app, oauth):
    c, csrf = await signed_in(app, 1)
    async with c:
        url = (await c.post(f"{API}/connectors/slack/connect", headers=csrf, json={})).json()["url"]
        cb = await c.get("/oauth/slack/callback", params={"error": "access_denied", "state": query(url)["state"]})
        assert cb.status_code == 303 and cb.headers["location"] == "/workspace?error=slack_denied"


async def test_connect_without_native_config_says_so(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    get_settings.cache_clear()
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.post(f"{API}/connectors/google/connect", headers=csrf, json={})
        assert r.status_code == 503 and r.json()["error"] == "not_configured"


async def test_disconnect_forgets_by_default_and_can_keep_what_was_learned(app, tokens, router, memory, data, monkeypatch):
    from mavis.domain.integrations import ConnectionState  # noqa: F401
    from mavis.tools.integrations.connect_flow import ConnectFlow
    from mavis.tools.integrations.connections import ConnectionCache
    from tests.tools.integrations.fakes import FakeBus, FakeState, Recorder

    rec = Recorder()
    flow = ConnectFlow(provider=router, cache=ConnectionCache(router, ttl_s=0), bus=FakeBus(), notify=rec.notify,
                       schedule=rec.schedule, state=FakeState(), base_url="https://mavis.test")
    import mavis.tools.integrations.wiring as wiring

    monkeypatch.setattr(wiring, "get_connect_flow", lambda: flow)
    for keep in (True, False):
        await tokens.save(1, G, account={"email": "a@x.com", "scopes": []}, access_token="a", refresh_token="r",
                          expires_at=None)
        c, csrf = await signed_in(app, 1)
        async with c:
            r = await c.post(f"{API}/connectors/google/disconnect", headers=csrf, json={"forget": not keep})
            assert r.status_code == 204, r.text
            assert (await c.get(f"{API}/connectors")).json()[0]["status"] == "none"
            assert (await c.post(f"{API}/connectors/google/disconnect", headers=csrf, json={})).status_code == 404
            gmail = [i for i in (await items(c, "organisation"))["items"] if i["source"] == "gmail"]
            assert bool(gmail) is keep  # forgotten with the connection unless the user kept it


# --- vault ------------------------------------------------------------------------------------------------


async def test_vault_items_map_to_ui_kinds_with_source_and_trust(app, data):
    c, _ = await signed_in(app, 1)
    async with c:
        pref = (await items(c, "preference"))["items"]
        assert [(i["title"], i["source"], i["trust"]) for i in pref] == [("Prefers zebra-quartz tea", "you", "user")]
        org = (await items(c, "organisation"))["items"]
        assert [(i["title"], i["source"], i["trust"]) for i in org] == [("Works at Acme Corp", "gmail", "high")]
        facts = (await items(c, "fact"))["items"]
        assert any(i["source"] == "gmail" and i["trust"] == "low" for i in facts)
        project = (await items(c, "project"))["items"]
        assert [i["title"] for i in project] == ["Goal: Ship the zebra launch"]
        s = (await c.get(f"{API}/vault/summary")).json()
        assert s["counts"]["organisations"] == 1 and s["counts"]["projects"] == 1 and s["counts"]["sources"] == 1
        assert s["profile"]["timezone"] and "token" not in str(s).lower()


async def test_vault_pagination_and_search(app, memory):
    for i in range(30):
        await learn(memory, 1, "KNOWS", f"Topic{i}", f"Fact number {i:02d}")
    c, _ = await signed_in(app, 1)
    async with c:
        first = (await c.get(f"{API}/vault/items", params={"kind": "fact"})).json()
        assert len(first["items"]) == 25 and first["next_cursor"] == "25"
        second = (await c.get(f"{API}/vault/items", params={"kind": "fact", "cursor": "25"})).json()
        assert len(second["items"]) == 5 and second["next_cursor"] is None
        hit = (await c.get(f"{API}/vault/items", params={"kind": "fact", "q": "number 07"})).json()
        assert [i["title"] for i in hit["items"]] == ["Fact number 07"]
        for bad in ({"kind": "nope"}, {"kind": "fact", "cursor": "-1"}):
            assert (await c.get(f"{API}/vault/items", params=bad)).status_code == 400


async def test_correcting_and_forgetting_an_item(app, data):
    c, csrf = await signed_in(app, 1)
    async with c:
        item = (await items(c, "preference"))["items"][0]
        r = await c.patch(f"{API}/vault/items/{item['id']}", headers=csrf,
                          json={"title": "Prefers green tea", "detail": None})
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Prefers green tea" and r.json()["source"] == "dashboard"
        titles = [i["title"] for i in (await items(c, "preference"))["items"]]
        assert titles == ["Prefers green tea"]
        new_id = r.json()["id"]
        assert (await c.request("DELETE", f"{API}/vault/items/{new_id}", headers=csrf,
                                json={"also_suppress": True})).status_code == 204
        assert (await items(c, "preference"))["items"] == []
        assert (await c.request("DELETE", f"{API}/vault/items/{new_id}", headers=csrf)).status_code == 404
        assert (await c.patch(f"{API}/vault/items/fact:doesnotexist", headers=csrf,
                              json={"title": "a real correction"})).status_code == 404
        assert (await c.patch(f"{API}/vault/items/{new_id}", headers=csrf, json={})).status_code == 400


async def test_profile_fields_are_edited_through_preferences(app):
    c, csrf = await signed_in(app, 1)
    async with c:
        for field, value in (("name", "Priya R"), ("timezone", "Asia/Tokyo"), ("currency", "jpy")):
            r = await c.patch(f"{API}/vault/items/profile.{field}", headers=csrf, json={"title": value})
            assert r.status_code == 200, r.text
        u = await users.get(1)
        assert (u.name, u.timezone, u.currency) == ("Priya R", "Asia/Tokyo", "JPY")
        assert (await c.patch(f"{API}/vault/items/profile.timezone", headers=csrf,
                              json={"title": "Mars/Base"})).status_code == 400
        assert (await c.patch(f"{API}/vault/items/profile.password", headers=csrf,
                              json={"title": "x"})).status_code == 404


async def test_sources_and_forgetting_a_source(app, data):
    c, csrf = await signed_in(app, 1)
    async with c:
        names = {s["source"] for s in (await c.get(f"{API}/vault/sources")).json()}
        assert {"you", "gmail"} <= names
        assert (await c.post(f"{API}/vault/sources/gmail/forget", headers=csrf, json={})).status_code == 204
        assert not [i for i in (await items(c, "organisation"))["items"]]
        assert (await c.post(f"{API}/vault/sources/you/forget", headers=csrf, json={})).status_code == 404


# --- invites ----------------------------------------------------------------------------------------------


async def test_invites_are_capped_by_tier_and_revocable(app):
    c, csrf = await signed_in(app, 1)  # standard tier: 3 links
    async with c:
        assert (await c.get(f"{API}/me")).json()["invites_left"] == 3
        made = []
        for n in range(3):
            r = await c.post(f"{API}/invites", headers=csrf, json={"name": f"Friend {n}"})
            assert r.status_code == 201
            made.append(r.json())
        assert made[0]["link"].startswith("https://mavis.test/?invite=MAV") and made[0]["max_uses"] == 5
        assert (await c.get(f"{API}/me")).json()["invites_left"] == 0
        full = await c.post(f"{API}/invites", headers=csrf, json={})
        assert full.status_code == 409 and full.json()["error"] == "invite_cap"
        listed = (await c.get(f"{API}/invites")).json()
        assert [i["name"] for i in listed] == ["Friend 0", "Friend 1", "Friend 2"] and listed[0]["link"] is None
        assert (await c.delete(f"{API}/invites/{listed[0]['code']}", headers=csrf)).status_code == 204
        assert (await c.get(f"{API}/me")).json()["invites_left"] == 1
        # the link that was made works as an invite
        plain = made[1]["link"].split("invite=")[1]
        assert await invites.redeem(plain, 20, timeutil.now()) is not None


async def test_the_owner_has_no_invite_cap(app):
    await users.update(1, tier="owner")
    c, csrf = await signed_in(app, 1)
    async with c:
        assert (await c.get(f"{API}/me")).json()["invites_left"] is None
        for _ in range(5):
            assert (await c.post(f"{API}/invites", headers=csrf, json={})).status_code == 201


async def test_concurrent_invite_creates_cannot_exceed_the_cap(app):
    import asyncio

    c, csrf = await signed_in(app, 1)  # standard tier: 3 links
    async with c:
        results = await asyncio.gather(*[c.post(f"{API}/invites", headers=csrf, json={}) for _ in range(8)])
        codes = sorted(r.status_code for r in results)
        assert codes == [201] * 3 + [409] * 5
        assert len((await c.get(f"{API}/invites")).json()) == 3


# --- connector consent is bound to the session that began it ----------------------------------------------


def google_grant_routes(vendor, email="victim@kripya.com"):
    vendor.routes[GOOGLE_TOKEN_URL] = lambda req: httpx.Response(200, json={
        "access_token": "ya29", "refresh_token": "1//r", "expires_in": 3600,
        "scope": "openid email https://www.googleapis.com/auth/gmail.readonly"})
    vendor.routes[GOOGLE_USERINFO_URL] = lambda req: httpx.Response(200, json={"sub": "9", "email": email})


async def test_a_forwarded_web_connect_link_saves_nothing_for_another_browser(app, oauth, vendor, tokens):
    google_grant_routes(vendor)
    attacker, csrf = await signed_in(app, 1)
    async with attacker, new_client(app) as victim:
        url = (await attacker.post(f"{API}/connectors/google/connect", headers=csrf, json={})).json()["url"]
        # the victim (no session, or another one) lands on the forwarded callback
        r = await victim.get("/oauth/google/callback", params={"code": "c", "state": query(url)["state"]})
        assert r.status_code == 400 and "Continue from the dashboard" in r.text
        assert await tokens.grant(1, NativeProvider.GOOGLE) is None
        # the state is spent, so the attacker's own browser cannot finish it either
        r = await attacker.get("/oauth/google/callback", params={"code": "c", "state": query(url)["state"]})
        assert await tokens.grant(1, NativeProvider.GOOGLE) is None
    # a different signed-in user (another session) is refused as well
        other, _ = await signed_in(app, 2)
        url = (await attacker.post(f"{API}/connectors/google/connect", headers=csrf, json={})).json()["url"]
        r = await other.get("/oauth/google/callback", params={"code": "c", "state": query(url)["state"]})
        assert r.status_code == 400 and await tokens.grant(1, NativeProvider.GOOGLE) is None


async def test_a_web_connect_finishes_in_the_session_that_began_it_and_never_confirms_the_email(app, oauth, vendor, tokens):
    google_grant_routes(vendor)
    c, csrf = await signed_in(app, 1)
    async with c:
        url = (await c.post(f"{API}/connectors/google/connect", headers=csrf, json={})).json()["url"]
        r = await c.get("/oauth/google/callback", params={"code": "c", "state": query(url)["state"]})
        assert r.status_code == 303 and await tokens.grant(1, NativeProvider.GOOGLE) is not None
    assert await emails.user_for("victim@kripya.com") is None


async def test_a_telegram_started_connect_link_still_works_without_a_session(app, oauth, vendor, tokens):
    google_grant_routes(vendor)
    state = query(await oauth.authorize_url(1, NativeProvider.GOOGLE))["state"]
    async with new_client(app) as c:
        r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
        assert r.status_code == 200 and "victim@kripya.com" in r.text
    assert await tokens.grant(1, NativeProvider.GOOGLE) is not None
    assert await emails.user_for("victim@kripya.com") is None  # and it is not a sign-in identity


# --- account deletion -------------------------------------------------------------------------------------


async def test_delete_account_needs_the_typed_word_and_ends_every_session(app, rec_bus):
    c, csrf = await signed_in(app, 1)
    other_browser, _ = await signed_in(app, 1)
    async with c, other_browser:
        r = await c.post(f"{API}/account/delete", headers=csrf, json={"confirm": "delete"})
        assert r.status_code == 400 and r.json()["error"] == "confirm"
        assert (await users.get(1)).status == "active"
        r = await c.post(f"{API}/account/delete", headers=csrf, json={"confirm": "DELETE"})
        assert r.status_code == 202
        assert (await users.get(1)).status == "deleting"
        assert [j.kind for j in rec_bus.jobs] == [JobKind.DELETE_USER] and rec_bus.jobs[0].user_id == 1
        assert (await c.get(f"{API}/me")).status_code == 401
        assert (await other_browser.get(f"{API}/me")).status_code == 401
    assert await sessions.count(1) == 0


async def test_the_deletion_cascade_removes_sessions_and_login_rows(app):
    from mavis.store.repo import deletion as repo

    await sessions.create(1)
    await emails.confirm(1, "gone@x.com", "google_grant")
    counts = await repo.delete_user_rows(1)
    assert counts["web_sessions"] == 1 and counts["user_emails"] == 1
    assert await emails.user_for("gone@x.com") is None


# --- isolation --------------------------------------------------------------------------------------------


async def test_a_session_never_reads_or_changes_the_other_users_data(app, data, tokens):
    a, csrf_a = await signed_in(app, 1)
    b, csrf_b = await signed_in(app, 2)
    async with a, b:
        b_items = (await items(b, "preference"))["items"]
        assert [i["title"] for i in b_items] == ["Prefers otter-lilac coffee"]
        b_id = b_items[0]["id"]
        # everything A can see mentions only A's markers
        everything = ""
        for k in ("person", "organisation", "project", "fact", "preference"):
            everything += str(await items(a, k))
        assert "otter-lilac" not in everything and "zebra" in everything
        assert "otter-lilac" not in (await a.get(f"{API}/vault/summary")).text
        # guessing B's item id: not found, nothing changes
        assert (await a.patch(f"{API}/vault/items/{b_id}", headers=csrf_a, json={"title": "hacked"})).status_code == 404
        assert (await a.request("DELETE", f"{API}/vault/items/{b_id}", headers=csrf_a)).status_code == 404
        assert [i["title"] for i in (await items(b, "preference"))["items"]] == ["Prefers otter-lilac coffee"]
        # B's invites and connectors are B's alone
        await tokens.save(2, G, account={"email": "b@x.com", "scopes": []}, access_token="t", refresh_token=None, expires_at=None)
        assert (await a.get(f"{API}/connectors")).json()[0]["status"] == "none"
        mine = (await b.post(f"{API}/invites", headers=csrf_b, json={"name": "B only"})).json()
        assert (await a.get(f"{API}/invites")).json() == []
        assert (await a.delete(f"{API}/invites/{mine['code']}", headers=csrf_a)).status_code == 404
        async with dbm.Session() as s:
            assert (await s.scalars(select(InviteCode).where(InviteCode.revoked_at.is_not(None)))).first() is None
        # preferences and /me are the session's own
        await a.patch(f"{API}/preferences", headers=csrf_a, json={"name": "A name"})
        assert (await b.get(f"{API}/preferences")).json()["name"] != "A name"
        # a copied session cookie of A never carries B's csrf
        assert (await a.patch(f"{API}/preferences", headers=csrf_b, json={"name": "Z"})).status_code == 403


async def test_no_handler_takes_a_user_id_from_the_request():
    """Every route depends on the session; none reads a user id or email from the path, query or body."""
    from mavis.api.dashboard import create_dashboard_app

    for route in create_dashboard_app().routes:
        path = getattr(route, "path", "")
        assert not re.search(r"user", path), path


def test_user_facing_text_has_no_em_or_en_dashes():
    root = Path(__file__).resolve().parents[2] / "src" / "mavis"
    files = [*(root / "api" / "dashboard").glob("*.py"), *(root / "web").glob("*.py"),
             root / "access" / "web_prefs.py"]
    for f in files:
        text = f.read_text()
        assert "—" not in text and "–" not in text, f
