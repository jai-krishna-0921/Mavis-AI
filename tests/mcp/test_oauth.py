"""MCP OAuth against a fake authorization server: discovery, registration, PKCE, tokens, refresh, issuer
binding and error hygiene."""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from mavis.connectors import netguard
from mavis.mcp import oauth as o
from mavis.mcp.failures import Kind, McpFailure
from tests.mcp.fake_auth import FakeAuthServer, s256
from tests.mcp.fake_server import FakeMcpServer, tool
from tests.mcp.localserver import serve


@asynccontextmanager
async def stack(**auth_kw):
    """An authorization server and an MCP server whose resource metadata points at it."""
    auth = FakeAuthServer(**auth_kw)
    mcp = FakeMcpServer(tools=[tool("get_x")], token="never-matches")
    async with serve(auth.app) as auth_base, serve(mcp.app) as mcp_base:
        auth.base = auth_base
        mcp.prm = {"resource": f"{mcp_base}/mcp", "authorization_servers": [auth_base],
                   "scopes_supported": ["read", "write"]}
        mcp.resource_metadata = f"{mcp_base}/.well-known/oauth-protected-resource"
        yield auth, mcp, f"{mcp_base}/mcp"


def challenge(mcp: FakeMcpServer) -> str:
    return f'Bearer resource_metadata="{mcp.resource_metadata}"'


# --- discovery ------------------------------------------------------------------------------------

async def test_discovery_follows_the_401_pointer(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        d = await o.McpOAuth().discover(url, www_authenticate=challenge(mcp))
    assert d.issuer == auth.base and d.resource == url
    assert d.authorization_endpoint == f"{auth.base}/authorize" and d.token_endpoint == f"{auth.base}/token"
    assert d.registration_endpoint == f"{auth.base}/register" and d.scopes == ("read", "write")


async def test_discovery_uses_the_well_known_locations_without_a_pointer(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        d = await o.McpOAuth().discover(url)
        paths = [r["path"] for r in mcp.requests]
    assert d.issuer == auth.base
    assert paths[0] == "/.well-known/oauth-protected-resource/mcp"  # the path-inserted form first


async def test_the_challenge_scope_wins_over_the_advertised_scopes(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        d = await o.McpOAuth().discover(url, www_authenticate=challenge(mcp) + ', scope="files:read"')
    assert d.scopes == ("files:read",)


async def test_openid_configuration_is_the_fallback_metadata_document(mcp_env) -> None:
    async with stack(metadata_path="/.well-known/openid-configuration") as (auth, mcp, url):
        d = await o.McpOAuth().discover(url)
    assert d.token_endpoint == f"{auth.base}/token"


async def test_a_server_with_no_resource_metadata_is_its_own_authorization_server(mcp_env) -> None:
    auth = FakeAuthServer()
    async with serve(auth.app) as base:
        auth.base = base
        d = await o.McpOAuth().discover(f"{base}/mcp")
    assert d.issuer == base and d.token_endpoint == f"{base}/token"


async def test_discovery_is_cached_for_ten_minutes(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        await client.discover(url)
        seen = len(auth.requests) + len(mcp.requests)
        await client.discover(url)
        assert len(auth.requests) + len(mcp.requests) == seen


async def test_a_different_issuer_in_the_metadata_is_refused(mcp_env) -> None:
    async with stack(issuer_override="https://elsewhere.example.com") as (auth, mcp, url):
        with pytest.raises(McpFailure) as err:
            await o.McpOAuth().discover(url)
    assert (err.value.kind, err.value.code) == (Kind.PERMANENT, "oauth_metadata")


async def test_endpoints_off_the_issuers_site_are_refused(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        auth.endpoint_host_override = auth.base.replace("127.0.0.1", "localhost")
        with pytest.raises(McpFailure) as err:
            await o.McpOAuth().discover(url)
    assert err.value.code == "oauth_not_same_site"


async def test_a_declared_host_may_hold_the_endpoints(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        auth.endpoint_host_override = auth.base.replace("127.0.0.1", "localhost")
        d = await o.McpOAuth().discover(url, allowed_hosts=("localhost",))
    assert d.token_endpoint.startswith("http://localhost:")


async def test_a_server_without_pkce_s256_is_refused(mcp_env) -> None:
    async with stack(pkce_methods=["plain"]) as (auth, mcp, url):
        with pytest.raises(McpFailure) as err:
            await o.McpOAuth().discover(url)
    assert err.value.code == "pkce_unsupported"


async def test_resource_metadata_for_another_server_is_refused(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        mcp.prm["resource"] = "https://other.example.com/mcp"
        with pytest.raises(McpFailure) as err:
            await o.McpOAuth().discover(url)
    assert err.value.code == "oauth_metadata"


async def test_a_server_that_offers_no_oauth_at_all_is_unsupported(mcp_env) -> None:
    mcp = FakeMcpServer()
    async with serve(mcp.app) as base:
        with pytest.raises(McpFailure) as err:
            await o.McpOAuth().discover(f"{base}/mcp")
    assert (err.value.kind, err.value.code) == (Kind.PERMANENT, "oauth_unsupported")


@pytest.mark.parametrize("pointer", [
    "http://10.0.0.5/.well-known/oauth-protected-resource", "https://169.254.169.254/latest/meta-data",
    "https://127.0.0.1/x", "ftp://a.example.com/x",
])
async def test_a_hostile_resource_metadata_pointer_is_refused_before_any_request(settings, pointer) -> None:
    with pytest.raises(McpFailure) as err:
        await o.McpOAuth().discover("https://mcp.example.com/mcp",
                                    www_authenticate=f'Bearer resource_metadata="{pointer}"')
    assert err.value.code == "oauth_metadata"


async def test_a_metadata_host_that_resolves_to_a_private_address_is_blocked(settings, monkeypatch) -> None:
    async def resolver(host, port):
        return ["10.0.0.9"]

    monkeypatch.setattr(netguard, "_resolver", resolver)
    with pytest.raises(McpFailure) as err:
        await o.McpOAuth().discover("https://mcp.example.com/mcp")
    assert err.value.code == "blocked_url"


@pytest.mark.parametrize("value", ["http://a.example.com/x", "https://user:pw@a.example.com/x", 5, None,
                                   "https://127.0.0.1/x", "https://a.example.com/" + "x" * 3000])
def test_endpoint_validation_rejects_unsafe_values(settings, value) -> None:
    with pytest.raises(McpFailure):
        o.McpOAuth._endpoint(value, "https://a.example.com", ())


# --- registration ---------------------------------------------------------------------------------

async def test_registration_is_a_public_client_with_our_redirect_uri(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        reg = await client.register(await client.discover(url))
        body = next(r["body"] for r in auth.requests if r["path"] == "/register")
    assert reg == o.Registration("client-1", None)
    assert body["token_endpoint_auth_method"] == "none" and body["client_name"] == "Mavis AI"
    assert body["redirect_uris"] == ["https://mavis.test/api/v1/mcp/oauth/callback"]
    assert body["grant_types"] == ["authorization_code", "refresh_token"] and body["scope"] == "read write"


async def test_a_server_without_registration_cannot_be_registered_with(mcp_env) -> None:
    async with stack(registration=False) as (auth, mcp, url):
        client = o.McpOAuth()
        with pytest.raises(McpFailure) as err:
            await client.register(await client.discover(url))
    assert err.value.code == "no_registration"


@pytest.mark.parametrize(("status", "kind"), [(500, Kind.TRANSIENT), (429, Kind.TRANSIENT),
                                              (400, Kind.PERMANENT), (403, Kind.PERMANENT)])
async def test_a_failed_registration_never_leaks_the_response(mcp_env, status, kind) -> None:
    async with stack(registration_status=status) as (auth, mcp, url):
        client = o.McpOAuth()
        with pytest.raises(McpFailure) as err:
            await client.register(await client.discover(url))
    assert err.value.kind is kind and "abc123" not in repr(err.value) + err.value.detail


# --- the consent redirect -------------------------------------------------------------------------

async def test_the_authorize_url_carries_pkce_state_resource_and_scope(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
    verifier = o.new_verifier()
    link = o.McpOAuth.authorize_url(facts, "state-xyz", verifier)
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert urlsplit(link).path == "/authorize"
    assert q == {"response_type": "code", "client_id": "client-1",
                 "redirect_uri": "https://mavis.test/api/v1/mcp/oauth/callback", "state": "state-xyz",
                 "code_challenge": s256(verifier), "code_challenge_method": "S256", "resource": url,
                 "scope": "read write"}
    assert 43 <= len(verifier) <= 128 and o.new_verifier() != verifier


# --- code exchange --------------------------------------------------------------------------------

async def test_the_code_is_exchanged_with_the_verifier_and_the_resource(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        verifier = o.new_verifier()
        code = auth.issue_code(s256(verifier))
        got = await client.exchange(facts, None, code, verifier)
        form = next(r["body"] for r in auth.requests if r["path"] == "/token")
    assert got.access_token == "access-1" and got.refresh_token == "refresh-2"
    assert got.expires_at is not None and got.scope is None
    assert form == {"grant_type": "authorization_code", "code": code, "redirect_uri":
                    "https://mavis.test/api/v1/mcp/oauth/callback", "code_verifier": verifier,
                    "client_id": "client-1", "resource": url}


async def test_a_client_secret_is_sent_when_there_is_one(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", "sealed", registration="dcr")
        verifier = o.new_verifier()
        await client.exchange(facts, "the-client-secret", auth.issue_code(s256(verifier)), verifier)
        assert next(r["body"] for r in auth.requests if r["path"] == "/token")["client_secret"] == (
            "the-client-secret")


async def test_a_wrong_verifier_or_a_replayed_code_is_refused(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        verifier = o.new_verifier()
        with pytest.raises(McpFailure) as err:
            await client.exchange(facts, None, auth.issue_code(s256(verifier)), "not-the-verifier")
        assert (err.value.kind, err.value.code) == (Kind.AUTH, "oauth_token")
        code = auth.issue_code(s256(verifier))
        await client.exchange(facts, None, code, verifier)
        with pytest.raises(McpFailure):
            await client.exchange(facts, None, code, verifier)  # codes are single use


@pytest.mark.parametrize(("status", "kind"), [(500, Kind.TRANSIENT), (503, Kind.TRANSIENT),
                                              (429, Kind.TRANSIENT), (401, Kind.AUTH), (418, Kind.PERMANENT)])
async def test_token_endpoint_failures_are_classified_without_leaking(mcp_env, status, kind) -> None:
    async with stack(token_status=status) as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        with pytest.raises(McpFailure) as err:
            await client.exchange(facts, "client-secret-777", "code", "verifier")
    assert err.value.kind is kind
    text = repr(err.value) + err.value.detail + str(err.value)
    assert "abc123" not in text and "client-secret-777" not in text and "verifier" not in text


def test_token_responses_are_validated() -> None:
    parse = o.McpOAuth._tokens
    ok = parse({"access_token": "a", "token_type": "bearer", "expires_in": "60", "refresh_token": "r",
                "scope": "x y"})
    assert (ok.access_token, ok.refresh_token, ok.scope) == ("a", "r", "x y") and ok.expires_at is not None
    assert parse({"access_token": "a"}).expires_at is None
    assert parse({"access_token": "a", "expires_in": -5}).expires_at is None
    assert parse({"access_token": "a", "expires_in": "soon"}).expires_at is None
    for bad in ({}, {"access_token": ""}, {"access_token": 5}, {"access_token": "a", "token_type": "mac"},
                {"access_token": "x" * 9000}, [], None):
        with pytest.raises(McpFailure):
            parse(bad)


# --- refresh and issuer binding -------------------------------------------------------------------

async def test_refresh_sends_the_refresh_token_and_returns_the_new_pair(mcp_env) -> None:
    async with stack(rotate_refresh=True) as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        got = await client.refresh(facts, None, "refresh-1")
        form = [r["body"] for r in auth.requests if r["path"] == "/token"][-1]
    assert got.access_token == "access-1" and got.refresh_token == "refresh-2"
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-1"
    assert form["resource"] == url and form["client_id"] == "client-1"


async def test_refresh_runs_discovery_with_the_401s_pointer(mcp_env) -> None:
    async with stack(rotate_refresh=True) as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        seen, real = {}, client.discover

        async def spy(server_url, **kw):
            seen.update(kw)
            return await real(server_url, **kw)

        client.discover = spy
        await client.refresh(facts, None, "refresh-1", www_authenticate=challenge(mcp))
    assert seen["www_authenticate"] == challenge(mcp)


async def test_a_refused_refresh_token_means_reconnect(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        with pytest.raises(McpFailure) as err:
            await client.refresh(facts, None, "revoked-token")
    assert (err.value.kind, err.value.code) == (Kind.AUTH, "oauth_token")


async def test_a_refresh_against_a_different_issuer_is_refused_and_sends_nothing(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        stored = (await o.McpOAuth().discover(url)).facts("client-1", None, registration="dcr")
        other = FakeAuthServer()
        async with serve(other.app) as other_base:
            other.base = other_base
            mcp.prm["authorization_servers"] = [other_base]  # the server now names another issuer
            with pytest.raises(McpFailure) as err:
                await o.McpOAuth().refresh(stored, None, "refresh-1")
    assert (err.value.kind, err.value.code) == (Kind.AUTH, "issuer_changed")
    assert not [r for r in auth.requests + other.requests if r["path"] == "/token"]


async def test_a_stored_token_endpoint_that_left_the_issuers_site_is_refused(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        facts["token_endpoint"] = "https://evil.example.net/token"
        with pytest.raises(McpFailure) as err:
            await client.refresh(facts, None, "refresh-1")
    assert err.value.code == "issuer_changed"
    assert not [r for r in auth.requests if r["path"] == "/token"]


# --- review fixes: cache keying, hostile numbers and JSON, RFC 9207 --------------------------------

async def test_the_discovery_cache_is_keyed_on_scope_and_hosts_and_is_bounded(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        first = await client.discover(url)
        stepped = await client.discover(url, scope="admin")
        assert first.scopes == ("read", "write") and stepped.scopes == ("admin",)
        assert (await client.discover(url, www_authenticate=challenge(mcp) + ', scope="x"')).scopes == ("x",)
        assert (await client.discover(url)).scopes == ("read", "write")
        seen = len(auth.requests) + len(mcp.requests)
        await client.discover(url, allowed_hosts=("localhost",))
        assert len(auth.requests) + len(mcp.requests) > seen  # different hosts, different entry
        o.DISCOVERY_CACHE_MAX, keep = 2, o.DISCOVERY_CACHE_MAX
        try:
            for i in range(5):
                await client.discover(url, scope=f"s{i}")
        finally:
            o.DISCOVERY_CACHE_MAX = keep
        assert len(client._cache) <= 3


async def test_a_failed_discovery_is_not_cached(mcp_env) -> None:
    async with stack(pkce_methods=["plain"]) as (auth, mcp, url):
        client = o.McpOAuth()
        for _ in range(2):
            with pytest.raises(McpFailure):
                await client.discover(url)
        assert not client._cache
        auth.pkce_methods = ["S256"]
        assert (await client.discover(url)).issuer == auth.base


@pytest.mark.parametrize("value", [float("inf"), 1e30, 10**30, float("nan"), True, "Infinity", "1e999"])
def test_hostile_expires_in_values_never_raise(value) -> None:
    got = o.McpOAuth._tokens({"access_token": "a", "expires_in": value})
    assert got.expires_at is None or got.expires_at < o.timeutil.now() + o.timedelta(days=366)


def test_deeply_nested_json_is_a_classified_failure() -> None:
    resp = o.httpx.Response(200, content=("[" * 100000).encode())
    assert o.McpOAuth._json(resp) is None
    with pytest.raises(McpFailure) as err:
        o.McpOAuth._tokens(o.McpOAuth._json(resp))
    assert err.value.code == "oauth_token"


def test_the_callback_issuer_is_checked() -> None:
    facts = {"issuer": "https://as.example.com"}
    o.McpOAuth.check_iss(facts, None)  # server never promised one
    o.McpOAuth.check_iss(facts, "https://as.example.com/")
    with pytest.raises(McpFailure) as err:
        o.McpOAuth.check_iss(facts, "https://evil.example.net")
    assert (err.value.kind, err.value.code) == (Kind.AUTH, "issuer_mismatch")
    required = {**facts, "iss_parameter_supported": True}
    with pytest.raises(McpFailure):
        o.McpOAuth.check_iss(required, None)
    o.McpOAuth.check_iss(required, "https://as.example.com")


async def test_exchange_refuses_a_wrong_issuer_before_any_request(mcp_env) -> None:
    async with stack() as (auth, mcp, url):
        client = o.McpOAuth()
        facts = (await client.discover(url)).facts("client-1", None, registration="dcr")
        verifier = o.new_verifier()
        code = auth.issue_code(s256(verifier))
        with pytest.raises(McpFailure) as err:
            await client.exchange(facts, None, code, verifier, iss="https://evil.example.net")
        assert err.value.code == "issuer_mismatch"
        assert not [r for r in auth.requests if r["path"] == "/token"]
        got = await client.exchange(facts, None, code, verifier, iss=auth.base)
        assert got.access_token == "access-1"
