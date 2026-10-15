from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import structlog.testing

from mavis.domain.policy import Capability
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import OAuthError, configured, verify_state
from mavis.tools.integrations.native.providers import (
    Exchange,
    ExchangeFailed,
    ProviderSpec,
    ScopeParam,
)

from .conftest import form

T = NativeProvider.TESTING
TOKEN = "https://idp.test/token"


def q(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _exchange(req):
    body = await req.post(TOKEN, data={"code": req.code, "code_verifier": req.verifier,
                                       "client_id": req.client_id})
    if not body.get("access_token"):
        raise ExchangeFailed
    return Exchange({"tid": "t1", "oid": "o1", "scopes": ["mail.read"]}, body["access_token"],
                    body.get("refresh_token"), None)


async def _revoke(req):
    await req.post("https://idp.test/revoke", data={"token": req.access_token})


def fake(**kw) -> ProviderSpec:
    base = dict(id="testing", display_name="Testco", brand="Testco", auth_url="https://idp.test/authorize",
                token_url=TOKEN, scope_params=(ScopeParam("scope", ("openid", "mail.read"), " "),),
                pkce=True, extra_auth_params={"prompt": "select_account"},
                client_id_setting="slack_client_id", client_secret_setting="slack_client_secret",
                exchange=_exchange, revoke=_revoke, account_key=lambda a: f"{a['tid']}/{a['oid']}",
                capabilities=(Capability.SANDBOX,))
    return ProviderSpec(**{**base, **kw})


async def test_configured_follows_the_spec_settings(native_env, isolated_registry):
    isolated_registry.register(fake())
    assert configured(T) is True
    isolated_registry.unregister("testing")
    isolated_registry.register(fake(client_id_setting="no_such_setting"))
    assert configured(T) is False


async def test_authorize_url_is_built_from_spec(oauth, isolated_registry):
    isolated_registry.register(fake())
    query = q(await oauth.authorize_url(1, T, 7))
    assert query["client_id"] == "sid" and query["scope"] == "openid mail.read"
    assert query["redirect_uri"] == "https://mavis.test/oauth/testing/callback"
    assert query["prompt"] == "select_account" and query["code_challenge_method"] == "S256"
    assert verify_state(query["state"]).provider == "testing"


async def test_no_pkce_means_no_challenge(oauth, isolated_registry):
    isolated_registry.register(fake(pkce=False))
    query = q(await oauth.authorize_url(1, T))
    assert "code_challenge" not in query and "response_type" not in query


async def test_complete_runs_the_exchange_hook_and_saves(oauth, vendor, isolated_registry):
    isolated_registry.register(fake())
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})
    state = q(await oauth.authorize_url(1, T, 7))["state"]
    done = await oauth.complete(state, "the-code", T)
    assert done.account["tid"] == "t1" and done.pending_id == 7
    sent = form(vendor.to(TOKEN)[0])
    assert sent["code"] == "the-code" and sent["code_verifier"] and sent["client_id"] == "sid"
    assert await oauth.tokens.owner_of_key(T, "t1/o1") == 1


async def test_exchange_failure_is_a_fixed_error(oauth, vendor, isolated_registry):
    isolated_registry.register(fake())
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={})
    state = q(await oauth.authorize_url(1, T))["state"]
    with pytest.raises(OAuthError) as e:
        await oauth.complete(state, "c", T)
    assert e.value.kind == "exchange_failed"


async def test_a_crashing_exchange_hook_becomes_a_fixed_error_without_vendor_text(
        oauth, vendor, isolated_registry):
    async def boom(req):
        raise KeyError("AADSTS-secret-vendor-claim")

    isolated_registry.register(fake(exchange=boom))
    state = q(await oauth.authorize_url(1, T))["state"]
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(OAuthError) as e:
            await oauth.complete(state, "c", T)
    assert e.value.kind == "exchange_failed" and e.value.user_id == 1
    assert "secret" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__
    [warn] = [x for x in logs if x["event"] == "oauth.exchange_hook_failed"]
    assert warn["error"] == "KeyError" and "secret" not in str(logs)


async def test_account_taken_uses_the_spec_key(oauth, vendor, isolated_registry):
    isolated_registry.register(fake())
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a"})
    await oauth.complete(q(await oauth.authorize_url(1, T))["state"], "c", T)
    with pytest.raises(OAuthError) as e:
        await oauth.complete(q(await oauth.authorize_url(2, T))["state"], "c", T)
    assert e.value.kind == "account_taken"


async def test_revoke_calls_the_hook_then_deletes(oauth, vendor, isolated_registry):
    isolated_registry.register(fake())
    vendor.routes[TOKEN] = lambda r: httpx.Response(200, json={"access_token": "a"})
    vendor.routes["https://idp.test/revoke"] = lambda r: httpx.Response(200, json={})
    await oauth.complete(q(await oauth.authorize_url(1, T))["state"], "c", T)
    await oauth.revoke(1, T)
    assert form(vendor.to("https://idp.test/revoke")[0])["token"] == "a"
    assert await oauth.tokens.grant(1, T) is None


async def test_unknown_and_internal_providers_are_not_configured(native_env):
    assert configured(NativeProvider.TESTING) is False
    assert configured(NativeProvider.SLACK_BOT) is False


async def test_google_and_slack_query_order_is_unchanged(oauth):
    google = list(parse_qs(urlsplit(await oauth.authorize_url(1, NativeProvider.GOOGLE)).query))
    assert google == ["response_type", "client_id", "redirect_uri", "scope", "state", "access_type",
                      "prompt", "include_granted_scopes", "code_challenge", "code_challenge_method"]
    slack = list(parse_qs(urlsplit(await oauth.authorize_url(1, NativeProvider.SLACK)).query))
    assert slack == ["client_id", "redirect_uri", "scope", "user_scope", "state"]
