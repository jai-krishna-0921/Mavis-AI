from __future__ import annotations

import httpx
import pytest
import structlog.testing
from fastapi import FastAPI
from sqlalchemy import select

from mavis.api.routes import oauth as oauth_route
from mavis.bus import get_bus
from mavis.domain.policy import Capability
from mavis.store import db as dbm
from mavis.store.models import OutboxMessage
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.providers import (
    Exchange,
    ExchangeFailed,
    ProviderSpec,
    ScopeParam,
)
from mavis.tools.integrations.native.router import NativeRouter
from tests.api.test_oauth_routes import query
from tests.tools.integrations.fakes import FakeBus, FakeProvider
from tests.tools.integrations.native.conftest import *  # noqa: F403 - fixtures

T = NativeProvider.TESTING
TOKEN = "https://idp.test/token"
remembered: list[tuple[int, dict]] = []


class _Exec:
    provider = T

    def handles(self, action):
        return False


async def _exchange(req):
    body = await req.post(TOKEN, data={"code": req.code})
    if not body.get("access_token"):
        raise ExchangeFailed
    return Exchange({"tid": "t1", "oid": "o1", "mail": "me@corp.test", "scopes": ["mail.read"]},
                    body["access_token"], None, None)


async def _remember(user_id, account):
    remembered.append((user_id, account))


async def _remember_boom(user_id, account):
    raise RuntimeError("graph is down")


def _denial(error, description):
    consent = error == "consent_required" or "AADSTS65001" in (description or "")
    return "admin_consent" if consent else "denied"


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", auth_url="https://idp.test/a",
                token_url=TOKEN, scope_params=(ScopeParam("scope", ("mail.read",)),), pkce=True,
                client_id_setting="slack_client_id", client_secret_setting="slack_client_secret",
                exchange=_exchange, account_key=lambda a: f"{a['tid']}/{a['oid']}",
                external_label=lambda a: a["mail"], remember_identity=_remember,
                capabilities=(Capability.NOTION, Capability.SANDBOX), anchor=Capability.NOTION,
                denial_kind=_denial, admin_link=lambda: "https://idp.test/adminconsent?client_id=sid",
                user_text={"admin_consent":
                           "{name} needs your admin to approve Mavis AI first: {admin_link}"})
    return ProviderSpec(**{**base, **kw})


@pytest.fixture
def web(tokens, oauth, client):
    bus = FakeBus()
    app = FastAPI()
    app.include_router(oauth_route.router)
    router = NativeRouter(FakeProvider(), tokens, oauth, [_Exec()], client)
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_provider] = lambda: router
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), bus


async def outbox():
    async with dbm.Session() as s:
        return [m.text for m in await s.scalars(select(OutboxMessage))]


async def test_success_page_identity_and_pending_come_from_the_spec(web, oauth, vendor, isolated_registry):
    isolated_registry.register(fake())
    c, bus = web
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a"})
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and "me@corp.test is now linked" in r.text
    assert remembered[-1][0] == 5 and remembered[-1][1]["mail"] == "me@corp.test"
    [job] = bus.jobs
    from mavis.store.repo import connections
    [pending] = await connections.open_for(5, Capability.NOTION)  # the anchor capability
    assert job.payload == {"pending_id": pending.id}
    assert any("Testco is connected: me@corp.test" in t for t in await outbox())


async def test_a_raising_identity_hook_does_not_fail_the_connect(web, oauth, vendor, isolated_registry):
    isolated_registry.register(fake(remember_identity=_remember_boom))
    c, bus = web
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a"})
    state = query(await oauth.authorize_url(5, T))["state"]
    with structlog.testing.capture_logs() as logs:
        r = await c.get("/oauth/testing/callback", params={"code": "c", "state": state})
    assert r.status_code == 200 and "me@corp.test is now linked" in r.text
    assert len(bus.jobs) == 1
    assert any("Testco is connected" in t for t in await outbox())
    [warn] = [e for e in logs if e["event"] == "oauth.identity_failed"]
    assert warn["log_level"] == "warning" and warn["error"] == "RuntimeError"


async def test_admin_consent_denial_tells_the_user_in_plain_words(web, oauth, isolated_registry):
    isolated_registry.register(fake())
    c, _ = web
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={
        "error": "access_denied", "error_description": "AADSTS65001 leaky vendor text", "state": state})
    assert r.status_code == 400 and "leaky" not in r.text
    assert "needs your admin to approve Mavis AI first: https://idp.test/adminconsent" in r.text
    [text] = await outbox()
    assert "adminconsent" in text and "leaky" not in text and "AADSTS" not in text


async def test_plain_denial_keeps_the_default_copy(web, oauth, isolated_registry):
    isolated_registry.register(fake())
    c, _ = web
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={"error": "access_denied", "state": state})
    assert r.status_code == 400 and "Testco was not connected" in r.text
    [text] = await outbox()
    assert text.startswith("Testco sign-in was cancelled")


async def test_nothing_allowed_uses_spec_features(web, oauth, vendor, isolated_registry):
    isolated_registry.register(fake(
        features=(("search", "notion.search"),), action_scopes={"notion.search": (frozenset({"zzz"}),)},
        nothing_allowed="You signed in with {account} but allowed nothing."))
    c, _ = web
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a"})
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={"code": "c", "state": state})
    assert r.status_code == 400 and "did not allow any Testco service" in r.text
    assert "You signed in with me@corp.test but allowed nothing." in await outbox()


async def test_internal_and_unknown_providers_are_404(web, isolated_registry):
    c, _ = web
    for name in ("slack_bot", "github"):
        assert (await c.get(f"/oauth/{name}/callback", params={"code": "c", "state": "s"})).status_code == 404
        assert (await c.get(f"/oauth/{name}/go", params={"t": "x"})).status_code == 404


async def test_a_raising_denial_hook_falls_back_to_plain_denied(web, oauth, isolated_registry):
    def boom(error, description):
        raise KeyError("vendor-claim")

    isolated_registry.register(fake(denial_kind=boom))
    c, _ = web
    state = query(await oauth.authorize_url(5, T))["state"]
    with structlog.testing.capture_logs() as logs:
        r = await c.get("/oauth/testing/callback", params={"error": "access_denied", "state": state})
    assert r.status_code == 400 and "Testco was not connected" in r.text
    [text] = await outbox()
    assert text.startswith("Testco sign-in was cancelled")
    [warn] = [e for e in logs if e["event"] == "oauth.denial_kind_failed"]
    assert warn["error"] == "KeyError" and "vendor-claim" not in str(logs)


async def test_an_unknown_placeholder_in_spec_copy_falls_back_to_the_default_text(
        web, oauth, isolated_registry):
    isolated_registry.register(fake(user_text={"admin_consent": "{name} needs {nope} approval"},
                                    denial_kind=lambda e, d: "admin_consent"))
    c, _ = web
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={"error": "access_denied", "state": state})
    assert r.status_code == 400 and "Testco was not connected" in r.text and "{nope}" not in r.text
    [text] = await outbox()
    assert "{nope}" not in text and text


async def test_a_failing_admin_link_never_raises_on_the_callback(web, oauth, isolated_registry):
    def boom():
        raise RuntimeError("no tenant")

    isolated_registry.register(fake(admin_link=boom))
    c, _ = web
    state = query(await oauth.authorize_url(5, T))["state"]
    r = await c.get("/oauth/testing/callback", params={
        "error": "access_denied", "error_description": "AADSTS65001", "state": state})
    assert r.status_code == 400
    [text] = await outbox()
    assert "https://" not in text and "approve Mavis AI first" in text
