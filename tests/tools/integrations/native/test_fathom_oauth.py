import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mavis.domain.policy import Capability as C
from mavis.tools.integrations.native import providers
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.fathom_spec import FATHOM_HOOK_SPEC, FATHOM_SPEC, hook_owner, hook_secret
from mavis.tools.integrations.native.oauth import NativeOAuth

from .conftest import form

F, H = NativeProvider.FATHOM, NativeProvider.FATHOM_HOOK
TOKEN = "https://api.fathom.ai/external/v1/oauth2/token"
HOOKS = "https://api.fathom.ai/external/v1/webhooks"


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.fixture
def oauth(tokens, client):
    return NativeOAuth(tokens, client)


def fathom_vendor(vendor, *, hook=None, expires_in=3600):
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={
        "access_token": "fa_access", "refresh_token": "fa_refresh", "expires_in": expires_in})
    vendor.routes[HOOKS] = hook or (lambda r: httpx.Response(201, json={"id": "wh1", "secret": "whsec_test"}))


async def connect(oauth, user=1):
    url = await oauth.authorize_url(user, F, None)
    return await oauth.complete(query(url)["state"], "code-1", F, None), query(url)


def test_specs_are_registered():
    assert providers.spec_of("fathom") is FATHOM_SPEC and providers.spec_of("fathom_hook") is FATHOM_HOOK_SPEC
    assert FATHOM_SPEC.capabilities == (C.FATHOM,) and FATHOM_SPEC.anchor is C.FATHOM
    assert FATHOM_SPEC.companions == ("fathom_hook",) and FATHOM_HOOK_SPEC.internal
    assert FATHOM_SPEC.token_auth == "body" and FATHOM_SPEC.gone_codes == frozenset({"invalid_grant"})
    assert FATHOM_SPEC.toolkits == frozenset({"fathom"})
    assert FATHOM_SPEC.executor == ("fathom", "FathomExecutor")
    assert FATHOM_SPEC.forget_prefixes == ("fathom:",) and FATHOM_SPEC.state_cursors == ("fathom_",)
    assert FATHOM_SPEC.memory_sources == {"fathom": ("fathom:",)}
    assert FATHOM_SPEC.pollers == {C.FATHOM: "mavis.tools.integrations.fathom_poll:poll_fathom"}


async def test_authorize_url(oauth):
    url = await oauth.authorize_url(1, F, None)
    assert url.startswith("https://fathom.video/external/v1/oauth2/authorize?")
    q = query(url)
    assert q["scope"] == "public_api" and q["response_type"] == "code" and q["client_id"] == "fid"
    assert "code_challenge" not in q


async def test_exchange_form_body_and_expiry(oauth, tokens, vendor):
    fathom_vendor(vendor)
    done, q = await connect(oauth)
    assert form(vendor.to(TOKEN)[0]) == {
        "grant_type": "authorization_code", "code": "code-1", "client_id": "fid",
        "client_secret": "fsecret", "redirect_uri": q["redirect_uri"]}
    assert done.account == {"scopes": ["public_api"]}
    grant = await tokens.grant(1, F)
    assert grant.expires_at is not None


async def test_after_save_creates_webhook_and_companion(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    req = vendor.to(HOOKS)[0]
    body = json.loads(req.content)
    route = body["destination_url"].rsplit("/", 1)[1]
    assert body == {"destination_url": f"https://mavis.test/webhooks/fathom/{route}",
                    "triggered_for": ["my_recordings"], "include_action_items": True,
                    "include_summary": True, "include_transcript": False, "include_crm_matches": False}
    assert req.headers["authorization"] == "Bearer fa_access"
    acct = await tokens.account(1, H)
    assert acct == {"webhook_id": "wh1", "route_token": route, "scopes": []}
    assert (await tokens.grant(1, H)).expires_at is None
    assert await hook_secret(tokens, 1) == "whsec_test"
    assert await hook_owner(tokens, route) == 1 and await hook_owner(tokens, "nope") is None


async def test_after_save_failure_keeps_connection(oauth, tokens, vendor):
    fathom_vendor(vendor, hook=lambda r: httpx.Response(403, json={"error": "nope"}))
    await connect(oauth)
    assert (await tokens.grant(1, F)).status == "ACTIVE"
    assert await tokens.grant(1, H) is None and await hook_secret(tokens, 1) is None


async def test_after_save_malformed_response_keeps_connection(oauth, tokens, vendor):
    fathom_vendor(vendor, hook=lambda r: httpx.Response(201, json={"id": "wh1"}))
    await connect(oauth)
    assert await tokens.grant(1, F) is not None and await tokens.grant(1, H) is None


async def test_trailing_slash_in_public_base_url_is_stripped(oauth, vendor, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mavis.test/")
    get_settings.cache_clear()
    fathom_vendor(vendor)
    await connect(oauth)
    assert json.loads(vendor.to(HOOKS)[0].content)["destination_url"].startswith("https://mavis.test/webhooks/")


async def test_reconnect_deletes_old_webhook_first(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    old_route = (await tokens.account(1, H))["route_token"]
    vendor.routes[HOOKS] = lambda r: httpx.Response(201, json={"id": "wh2", "secret": "whsec_two"})
    vendor.routes[HOOKS + "/wh1"] = lambda r: httpx.Response(204)
    vendor.requests.clear()
    await connect(oauth)
    assert [(r.method, r.url.path) for r in vendor.requests if "webhooks" in r.url.path] == [
        ("DELETE", "/external/v1/webhooks/wh1"), ("POST", "/external/v1/webhooks")]
    acct = await tokens.account(1, H)
    assert acct["webhook_id"] == "wh2" and acct["route_token"] != old_route
    assert await hook_secret(tokens, 1) == "whsec_two"


async def test_disconnect_deletes_webhook_and_companion(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    vendor.routes[HOOKS + "/wh1"] = lambda r: httpx.Response(204)
    await oauth.revoke(1, F)
    dels = [r for r in vendor.requests if r.method == "DELETE"]
    assert len(dels) == 1 and dels[0].url.path == "/external/v1/webhooks/wh1"
    assert dels[0].headers["authorization"] == "Bearer fa_access"
    assert await tokens.grant(1, F) is None and await tokens.grant(1, H) is None


async def test_disconnect_with_vendor_failure_still_removes_rows(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    vendor.routes[HOOKS + "/wh1"] = lambda r: httpx.Response(500)
    await oauth.revoke(1, F)
    assert await tokens.grant(1, F) is None and await tokens.grant(1, H) is None


async def test_disconnect_without_companion_just_deletes(oauth, tokens, vendor):
    fathom_vendor(vendor, hook=lambda r: httpx.Response(403))
    await connect(oauth)
    await oauth.revoke(1, F)
    assert not [r for r in vendor.requests if r.method == "DELETE"]
    assert await tokens.grant(1, F) is None


async def _expire(user=1):
    from datetime import timedelta

    from sqlalchemy import update

    from mavis.domain import timeutil
    from mavis.store import db as dbm
    from mavis.store.models import NativeGrant

    async with dbm.Session() as s:
        await s.execute(update(NativeGrant).where(
            NativeGrant.user_id == user, NativeGrant.provider == "fathom").values(
            expires_at=timeutil.now() - timedelta(hours=1)))
        await s.commit()


async def test_disconnect_refreshes_an_expired_token_before_deleting(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    await _expire()
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={
        "access_token": "fa_new", "refresh_token": "fa_refresh2", "expires_in": 3600})
    vendor.routes[HOOKS + "/wh1"] = lambda r: httpx.Response(
        204 if r.headers["authorization"] == "Bearer fa_new" else 401)
    await oauth.revoke(1, F)
    dels = [r for r in vendor.requests if r.method == "DELETE"]
    assert [d.headers["authorization"] for d in dels] == ["Bearer fa_new"]
    assert await tokens.grant(1, F) is None and await tokens.grant(1, H) is None


async def test_disconnect_retries_once_with_a_refreshed_token_on_401(oauth, tokens, vendor):
    fathom_vendor(vendor)
    await connect(oauth)
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={
        "access_token": "fa_new", "refresh_token": "fa_refresh2", "expires_in": 3600})
    vendor.routes[HOOKS + "/wh1"] = lambda r: httpx.Response(
        204 if r.headers["authorization"] == "Bearer fa_new" else 401)
    await oauth.revoke(1, F)
    dels = [r for r in vendor.requests if r.method == "DELETE"]
    assert [d.headers["authorization"] for d in dels] == ["Bearer fa_access", "Bearer fa_new"]
