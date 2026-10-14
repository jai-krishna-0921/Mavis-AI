# ruff: noqa: E501
"""The dashboard MCP API: session and CSRF rules, validation, OAuth consent, isolation, the flag."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from mavis.config import get_settings
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import new_client, signed_in
from tests.mcp.fake_auth import FakeAuthServer
from tests.mcp.test_service import oauth_server, open_server

API = "/api/v1"


@pytest.fixture
def mcp_on(dash_env, monkeypatch):
    """MCP_ENABLED and the dev-only localhost allowance (the fake servers are on 127.0.0.1)."""
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_ALLOW_LOCALHOST", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def add(c, csrf, **body):
    return await c.post(f"{API}/mcp/servers", headers=csrf, json=body)


# --- the flag, the session and CSRF -------------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/mcp/catalog"), ("GET", "/mcp/servers"), ("POST", "/mcp/servers"),
    ("POST", "/mcp/servers/1/connect"), ("POST", "/mcp/servers/1/refresh"), ("DELETE", "/mcp/servers/1"),
    ("GET", "/mcp/oauth/callback?code=x&state=y"),
])
async def test_every_route_is_a_404_while_the_flag_is_off(app, method, path) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await c.request(method, API + path, headers=csrf, json={} if method == "POST" else None)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_the_public_config_reports_the_flag(app, mcp_on, monkeypatch) -> None:
    async with new_client(app) as c:
        assert (await c.get(f"{API}/config")).json()["mcp_enabled"] is True
        monkeypatch.setenv("MCP_ENABLED", "false")
        get_settings.cache_clear()
        assert (await c.get(f"{API}/config")).json()["mcp_enabled"] is False


async def test_routes_need_a_session_and_writes_need_the_csrf_header(app, mcp_on) -> None:
    async with new_client(app) as anon:
        assert (await anon.get(f"{API}/mcp/servers")).status_code == 401
        assert (await anon.post(f"{API}/mcp/servers", json={})).status_code == 401
    c, csrf = await signed_in(app, 1)
    async with c:
        assert (await c.post(f"{API}/mcp/servers", json={"catalog_id": "x"})).status_code == 403
        assert (await c.delete(f"{API}/mcp/servers/1")).status_code == 403
        assert (await c.post(f"{API}/mcp/servers/1/refresh", json={})).status_code == 403
        assert (await c.get(f"{API}/mcp/servers")).status_code == 200


async def test_the_catalog_lists_the_apps_without_secrets(app, mcp_on) -> None:
    c, _ = await signed_in(app, 1)
    async with c:
        body = (await c.get(f"{API}/mcp/catalog")).json()
    assert 1 <= len(body) <= 12 and set(body[0]) == {"id", "name", "description", "auth", "url"}


# --- adding ------------------------------------------------------------------------------------------------------------

async def test_a_custom_server_is_added_probed_and_listed(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, open_server(token="paste-1") as (fake, url):
        r = await add(c, csrf, name="My Notes", url=url, bearer_token="paste-1")
        assert r.status_code == 201 and set(r.json()) == {"server"}
        server = r.json()["server"]
        assert (server["slug"], server["status"], server["auth"], server["tool_count"]) == ("my_notes", "ACTIVE", "bearer", 2)
        listed = (await c.get(f"{API}/mcp/servers")).json()
        assert [s["id"] for s in listed] == [server["id"]] and listed[0]["tools"][0]["kind"] in ("read", "act")
        assert "paste-1" not in r.text and "paste-1" not in str(listed) and "/mcp" not in server["host"]


@pytest.mark.parametrize(("body", "code"), [
    ({"name": "x", "url": "http://10.0.0.5/mcp"}, "invalid_url"),
    ({"name": " ", "url": "https://mcp.example.com/mcp"}, "invalid_name"),
    ({"catalog_id": "no-such"}, "invalid_catalog_id"),
    ({"name": "x", "url": "https://mcp.example.com/mcp", "bearer_token": "a b"}, "invalid_bearer_token"),
])
async def test_bad_input_is_a_422_with_a_plain_message(app, mcp_on, body, code) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await add(c, csrf, **body)
    assert r.status_code == 422 and r.json()["error"] == code and r.json()["message"]


async def test_unknown_fields_are_refused(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        r = await add(c, csrf, name="x", url="https://mcp.example.com/mcp", user_id=2)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


async def test_a_server_that_does_not_answer_is_a_probe_failure_without_leaking_its_reply(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        async with open_server(always_status=404) as (fake, url):
            r = await add(c, csrf, name="Nothing", url=url)
            assert (r.status_code, r.json()["error"]) == (422, "probe_failed")
            assert "not found" in r.json()["message"] and "boom" not in r.text and "abc123" not in r.text
        async with open_server(always_status=503) as (fake, url):
            assert (await add(c, csrf, name="Down", url=url)).status_code == 502
        assert (await c.get(f"{API}/mcp/servers")).json() == []


async def test_the_same_address_twice_and_the_eleventh_server_are_409s(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, open_server() as (fake, url):
        assert (await add(c, csrf, name="A", url=url)).status_code == 201
        dup = await add(c, csrf, name="B", url=url)
        assert (dup.status_code, dup.json()["error"]) == (409, "duplicate")
        for i in range(9):
            assert (await add(c, csrf, name=f"S{i}", url=f"{url}?n={i}")).status_code == 201
        full = await add(c, csrf, name="Eleven", url=f"{url}?n=99")
        assert (full.status_code, full.json()["error"]) == (409, "limit_reached")


async def test_adding_is_rate_limited_per_session(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        codes = [(await add(c, csrf, name="x", url="http://bad")).status_code for _ in range(14)]
    assert codes[:12] == [422] * 12 and codes[12:] == [429, 429]


# --- OAuth consent ---------------------------------------------------------------------------------------------------------

def consent(auth: FakeAuthServer, link: str) -> tuple[str, str]:
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    return q["state"], auth.issue_code(q["code_challenge"], q["client_id"])


async def test_oauth_consent_end_to_end(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, oauth_server() as (auth, fake, url):
        r = await add(c, csrf, name="Linear", url=url)
        assert r.status_code == 201 and r.json()["server"]["status"] == "NEEDS_AUTH"
        state, code = consent(auth, r.json()["authorize_url"])
        back = await c.get(f"{API}/mcp/oauth/callback", params={"code": code, "state": state})
        assert back.status_code == 303 and back.headers["location"] == "/workspace?mcp=connected"
        server = (await c.get(f"{API}/mcp/servers")).json()[0]
        assert (server["status"], server["tool_count"], server["auth"]) == ("ACTIVE", 2, "oauth")
        # a second use of the same state is an error and changes nothing
        again = await c.get(f"{API}/mcp/oauth/callback", params={"code": code, "state": state})
        assert again.headers["location"] == "/workspace?mcp=error"


async def test_the_callback_passes_the_issuer_parameter_on(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, oauth_server() as (auth, fake, url):
        r = await add(c, csrf, name="Linear", url=url)
        state, code = consent(auth, r.json()["authorize_url"])
        bad = await c.get(f"{API}/mcp/oauth/callback",
                          params={"code": code, "state": state, "iss": "https://evil.example.com"})
        assert bad.headers["location"] == "/workspace?mcp=error"
        assert (await c.get(f"{API}/mcp/servers")).json()[0]["status"] == "NEEDS_AUTH"
        state2, code2 = consent(auth, (await c.post(f"{API}/mcp/servers/{r.json()['server']['id']}/connect",
                                                    headers=csrf, json={})).json()["authorize_url"])
        good = await c.get(f"{API}/mcp/oauth/callback", params={"code": code2, "state": state2, "iss": auth.base})
        assert good.headers["location"] == "/workspace?mcp=connected"


async def test_a_stolen_state_reveals_nothing_about_its_owner(app, mcp_on) -> None:
    c1, csrf1 = await signed_in(app, 1)
    c2, _ = await signed_in(app, 2)
    async with c1, c2, oauth_server() as (auth, fake, url):
        r = await add(c1, csrf1, name="Linear", url=url)
        state, code = consent(auth, r.json()["authorize_url"])
        out = await c2.get(f"{API}/mcp/oauth/callback", params={"code": code, "state": state})
        assert out.status_code == 303 and out.text == "" and set(out.headers["location"].split("?")) == {"/workspace", "mcp=error"}


async def test_another_users_browser_cannot_finish_my_consent(app, mcp_on) -> None:
    c1, csrf1 = await signed_in(app, 1)
    c2, _ = await signed_in(app, 2)
    async with c1, c2, oauth_server() as (auth, fake, url):
        r = await add(c1, csrf1, name="Linear", url=url)
        state, code = consent(auth, r.json()["authorize_url"])
        stolen = await c2.get(f"{API}/mcp/oauth/callback", params={"code": code, "state": state})
        assert stolen.headers["location"] == "/workspace?mcp=error"
        assert (await c1.get(f"{API}/mcp/servers")).json()[0]["status"] == "NEEDS_AUTH"
        assert (await c2.get(f"{API}/mcp/servers")).json() == []


async def test_the_callback_without_a_session_or_with_a_denial_goes_back_with_an_error(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, oauth_server() as (auth, fake, url):
        r = await add(c, csrf, name="Linear", url=url)
        state, code = consent(auth, r.json()["authorize_url"])
        async with new_client(app) as anon:
            out = await anon.get(f"{API}/mcp/oauth/callback", params={"code": code, "state": state})
        assert out.headers["location"] == "/workspace?mcp=error"
        state2, code2 = consent(auth, (await c.post(f"{API}/mcp/servers/{r.json()['server']['id']}/connect", headers=csrf, json={})).json()["authorize_url"])
        denied = await c.get(f"{API}/mcp/oauth/callback", params={"error": "access_denied: <b>bad</b>", "state": state2})
        assert denied.headers["location"] == "/workspace?mcp=error" and "bad" not in denied.headers["location"]
        late = await c.get(f"{API}/mcp/oauth/callback", params={"code": code2, "state": state2})
        assert late.headers["location"] == "/workspace?mcp=error"  # the denial spent the state


async def test_garbage_callbacks_are_harmless(app, mcp_on) -> None:
    c, _ = await signed_in(app, 1)
    async with c:
        for params in ({}, {"code": "x"}, {"state": "y"}, {"code": "x", "state": "y" * 200}):
            r = await c.get(f"{API}/mcp/oauth/callback", params=params)
            assert (r.status_code, r.headers["location"]) == (303, "/workspace?mcp=error")


async def test_connect_gives_a_fresh_link_for_oauth_and_takes_a_token_for_the_others(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c:
        async with oauth_server() as (auth, fake, url):
            oauth_id = (await add(c, csrf, name="Linear", url=url)).json()["server"]["id"]
            r = await c.post(f"{API}/mcp/servers/{oauth_id}/connect", headers=csrf, json={})
            assert r.status_code == 200 and urlsplit(r.json()["authorize_url"]).path == "/authorize"
        async with open_server(token="old") as (fake, url):
            bid = (await add(c, csrf, name="Tok", url=url, bearer_token="old")).json()["server"]["id"]
            fake.token = "new"
            stale = await c.post(f"{API}/mcp/servers/{bid}/refresh", headers=csrf, json={})
            assert stale.json()["status"] == "NEEDS_AUTH"
            no_token = await c.post(f"{API}/mcp/servers/{bid}/connect", headers=csrf, json={})
            assert (no_token.status_code, no_token.json()["error"]) == (422, "invalid_bearer_token")
            wrong = await c.post(f"{API}/mcp/servers/{bid}/connect", headers=csrf,
                                 json={"bearer_token": "nope"})
            assert (wrong.status_code, wrong.json()["error"]) == (422, "probe_failed")
            ok = await c.post(f"{API}/mcp/servers/{bid}/connect", headers=csrf, json={"bearer_token": "new"})
            assert ok.status_code == 200 and ok.json()["server"]["status"] == "ACTIVE"
            assert '"new"' not in ok.text


# --- refresh, delete, isolation ------------------------------------------------------------------------------------------------

async def test_refresh_and_delete(app, mcp_on) -> None:
    c, csrf = await signed_in(app, 1)
    async with c, open_server() as (fake, url):
        sid = (await add(c, csrf, name="Notes", url=url)).json()["server"]["id"]
        from tests.mcp.fake_server import tool

        fake.tools.append(tool("list_notes", read_only=True))
        assert (await c.post(f"{API}/mcp/servers/{sid}/refresh", headers=csrf, json={})).json()["tool_count"] == 3
        fake.always_status = 500
        failed = await c.post(f"{API}/mcp/servers/{sid}/refresh", headers=csrf, json={})
        assert failed.status_code == 200 and failed.json()["status"] == "ACTIVE"  # a hiccup changes nothing
        assert (await c.delete(f"{API}/mcp/servers/{sid}", headers=csrf)).status_code == 204
        assert (await c.delete(f"{API}/mcp/servers/{sid}", headers=csrf)).status_code == 404
        assert (await c.get(f"{API}/mcp/servers")).json() == []


async def test_one_user_cannot_see_or_touch_anothers_server(app, mcp_on) -> None:
    c1, csrf1 = await signed_in(app, 1)
    c2, csrf2 = await signed_in(app, 2)
    async with c1, c2, open_server() as (fake, url):
        sid = (await add(c1, csrf1, name="Notes", url=url)).json()["server"]["id"]
        assert (await c2.get(f"{API}/mcp/servers")).json() == []
        for method, path, body in (("POST", f"/mcp/servers/{sid}/connect", {}), ("POST", f"/mcp/servers/{sid}/refresh", {}),
                                   ("DELETE", f"/mcp/servers/{sid}", None)):
            r = await c2.request(method, API + path, headers=csrf2, json=body)
            assert r.status_code == 404, path
        assert (await c1.get(f"{API}/mcp/servers")).json()[0]["id"] == sid
        again = await add(c2, csrf2, name="Notes", url=url)  # user 2 may add the same address
        assert again.status_code == 201 and again.json()["server"]["id"] != sid
