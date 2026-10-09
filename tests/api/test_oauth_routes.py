import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from mavis.api.routes import oauth as oauth_route
from mavis.bus import get_bus
from mavis.domain.events import JobKind
from mavis.store import db as dbm
from mavis.store.models import OutboxMessage
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.router import NativeRouter
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL
from tests.tools.integrations.fakes import FakeBus, FakeProvider
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures
from tests.tools.integrations.native.test_oauth import google_vendor, query, slack_vendor

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK


@pytest.fixture
def web(tokens, oauth, client):
    bus = FakeBus()
    app = FastAPI()
    app.include_router(oauth_route.router)
    router = NativeRouter(FakeProvider(), tokens, oauth, [], client)
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_provider] = lambda: router
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return c, bus


async def outbox():
    async with dbm.Session() as s:
        return [m.text for m in await s.scalars(select(OutboxMessage))]


async def test_google_success_enqueues_the_connection_check(web, oauth, vendor):
    c, bus = web
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G, 12))["state"]
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and "me@kripya.com is now linked" in r.text
    [job] = bus.jobs
    assert job.kind is JobKind.CONNECTION_CHECK and job.user_id == 5 and job.payload == {"pending_id": 12}


async def test_slack_success_without_a_pending_tells_the_user(web, oauth, vendor):
    c, bus = web
    slack_vendor(vendor)
    state = query(await oauth.authorize_url(5, S))["state"]
    r = await c.get("/oauth/slack/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and bus.jobs == []
    [text] = await outbox()
    assert text.startswith("Slack is connected") and "Kripya" in text


async def test_denied_says_so_plainly_and_echoes_nothing(web, oauth):
    c, bus = web
    state = query(await oauth.authorize_url(5, G, 12))["state"]
    r = await c.get("/oauth/google/callback", params={"error": "access_denied<script>", "state": state})
    assert r.status_code == 400 and "script" not in r.text and "access_denied" not in r.text
    [text] = await outbox()
    assert "cancelled" in text and "access_denied" not in text and bus.jobs == []


async def test_replayed_state_is_refused(web, oauth, vendor):
    c, bus = web
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G, 12))["state"]
    await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 400 and len(bus.jobs) == 1
    assert any("already used" in t for t in await outbox())


@pytest.mark.parametrize("params", [{}, {"code": "c"}, {"state": "junk"}, {"code": "c", "state": "a.b"},
                                    {"error": "access_denied"}])
async def test_garbage_returns_never_crash_or_notify(web, params):
    c, bus = web
    r = await c.get("/oauth/google/callback", params=params)
    assert r.status_code == 400 and bus.jobs == [] and await outbox() == []


async def test_exchange_failure_notifies_without_vendor_text(web, oauth, vendor):
    c, _ = web
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "leaky"})
    state = query(await oauth.authorize_url(5, G, 12))["state"]
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 400 and "leaky" not in r.text
    [text] = await outbox()
    assert "leaky" not in text and "invalid_grant" not in text


async def test_unknown_provider_and_composio_mode_are_404(web, tokens):
    c, _ = web
    assert (await c.get("/oauth/github/callback", params={"code": "c", "state": "s"})).status_code == 404
    app = FastAPI()
    app.include_router(oauth_route.router)
    app.dependency_overrides[get_provider] = lambda: FakeProvider()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c2:
        assert (await c2.get("/oauth/google/callback", params={"code": "c", "state": "s"})).status_code == 404


async def test_no_em_or_en_dashes_in_user_text():
    for text in oauth_route._USER_TEXT.values():
        assert "—" not in text and "–" not in text


async def named_user(name):
    from mavis.store.repo import users

    u, _ = await users.get_or_create_by_chat(777, name)
    return u


async def test_the_success_page_names_the_mavis_account_and_the_google_account(web, oauth, vendor, db):
    c, _ = web
    google_vendor(vendor, email="victim@kripya.com")
    u = await named_user("Priya <b>Nair</b>")
    state = query(await oauth.authorize_url(u.id, G))["state"]
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 200
    assert "victim@kripya.com" in r.text and "Priya &lt;b&gt;Nair&lt;/b&gt;" in r.text
    assert "<b>" not in r.text  # the display name is escaped, it is the other person's text
    assert "not you" in r.text  # tells a mistaken consenter what to do


async def test_the_telegram_confirmation_names_the_google_account(web, oauth, vendor, db):
    c, _ = web
    google_vendor(vendor, email="me@kripya.com")
    u = await named_user("Priya")
    state = query(await oauth.authorize_url(u.id, G, 4))["state"]  # even with a pending connect
    await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    [text] = await outbox()
    assert "me@kripya.com" in text and "—" not in text and "–" not in text


async def test_the_slack_page_and_confirmation_name_the_workspace(web, oauth, vendor, db):
    c, _ = web
    slack_vendor(vendor)
    u = await named_user("Priya")
    state = query(await oauth.authorize_url(u.id, S))["state"]
    r = await c.get("/oauth/slack/callback", params={"code": "c", "state": state})
    assert "Kripya" in r.text and "Priya" in r.text
    [text] = await outbox()
    assert "Kripya" in text


async def test_a_user_without_a_name_is_still_identified(web, oauth, vendor, db):
    c, _ = web
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G))["state"]  # no such user row
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and "Mavis account" in r.text


async def test_a_consent_link_dies_after_ten_minutes_and_after_one_use(oauth, vendor, db):
    import time

    from mavis.store.models import NativeOAuthState
    from mavis.tools.integrations.native.oauth import STATE_TTL_S, verify_state

    assert STATE_TTL_S <= 600
    state = query(await oauth.authorize_url(5, G))["state"]
    assert verify_state(state).exp - time.time() <= 600
    async with dbm.Session() as s:
        [row] = await s.scalars(select(NativeOAuthState))
    from mavis.domain import timeutil
    assert (timeutil.ensure_utc(row.expires_at) - timeutil.now()).total_seconds() <= 600


async def test_a_google_connect_teaches_the_users_address(web, oauth, vendor, user):
    from mavis.attention.connector_ingest import load_identities

    c, _ = web
    google_vendor(vendor, email="Jai.K@Kripya.com")
    state = query(await oauth.authorize_url(user.id, G, 12))["state"]
    assert (await c.get("/oauth/google/callback", params={"code": "c", "state": state})).status_code == 200
    assert (await load_identities(user.id))["emails"] == ["jai.k@kripya.com"]


async def test_a_slack_connect_teaches_the_users_slack_id_and_workspace(web, oauth, vendor, user):
    from mavis.attention.connector_ingest import load_identities

    c, _ = web
    slack_vendor(vendor)
    state = query(await oauth.authorize_url(user.id, S))["state"]
    assert (await c.get("/oauth/slack/callback", params={"code": "c", "state": state})).status_code == 200
    ident = await load_identities(user.id)
    assert ident["slack_ids"] == ["U1"] and ident["team"] == "T1"


async def test_a_failure_teaching_the_identity_does_not_undo_the_connection(web, oauth, vendor, user, monkeypatch):
    from mavis.attention import connector_ingest

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(connector_ingest, "remember_identity", boom)
    c, bus = web
    google_vendor(vendor)
    state = query(await oauth.authorize_url(user.id, G, 12))["state"]
    r = await c.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and len(bus.jobs) == 1
