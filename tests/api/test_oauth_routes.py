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
    assert r.status_code == 200 and "Google is connected" in r.text
    [job] = bus.jobs
    assert job.kind is JobKind.CONNECTION_CHECK and job.user_id == 5 and job.payload == {"pending_id": 12}


async def test_slack_success_without_a_pending_tells_the_user(web, oauth, vendor):
    c, bus = web
    slack_vendor(vendor)
    state = query(await oauth.authorize_url(5, S))["state"]
    r = await c.get("/oauth/slack/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and bus.jobs == []
    assert await outbox() == ["Slack is connected."]


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
