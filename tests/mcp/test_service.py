"""The MCP server service against fake MCP and authorization servers: creating, probing, OAuth consent,
status transitions, resolving and calling tools, limits and isolation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from mavis.mcp import catalog, sealing
from mavis.mcp import service as svc
from mavis.mcp.failures import Failure, Kind, McpFailure
from mavis.mcp.service import McpService
from mavis.store.repo import mcp_servers as repo
from tests.mcp.fake_auth import FakeAuthServer
from tests.mcp.fake_server import FakeMcpServer, text_result, tool
from tests.mcp.localserver import serve

NOTES = [tool("get_note", "Read a note", {"id": {"type": "string"}}, ["id"], read_only=True),
         tool("create_note", "Make a note", {"title": {"type": "string"}}, ["title"])]


def handlers() -> dict:
    return {"get_note": lambda a: text_result(f"note {a.get('id')}"),
            "create_note": lambda a: text_result(f"made {a.get('title')}")}


@asynccontextmanager
async def open_server(**kw):
    kw.setdefault("tools", list(NOTES))
    kw.setdefault("handlers", handlers())
    fake = FakeMcpServer(**kw)
    async with serve(fake.app) as base:
        yield fake, f"{base}/mcp"


@asynccontextmanager
async def oauth_server():
    """An MCP server that wants OAuth, and the authorization server it names."""
    auth = FakeAuthServer()
    fake = FakeMcpServer(tools=list(NOTES), handlers=handlers(), token="access-1")
    async with serve(auth.app) as auth_base, serve(fake.app) as base:
        auth.base = auth_base
        fake.prm = {"resource": f"{base}/mcp", "authorization_servers": [auth_base]}
        fake.resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        yield auth, fake, f"{base}/mcp"


def consent(auth: FakeAuthServer, link: str) -> tuple[str, str]:
    """Play the browser: approve the consent URL; returns (state, code)."""
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert q["code_challenge_method"] == "S256" and q["response_type"] == "code"
    return q["state"], auth.issue_code(q["code_challenge"], q["client_id"])


@pytest.fixture
def service(mcp_env) -> McpService:
    return McpService()


# --- creating -------------------------------------------------------------------------------------

async def test_a_custom_open_server_is_probed_and_its_tools_kept(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="My  Notes", url=url)
    v = made.server
    assert made.authorize_url is None
    assert (v["slug"], v["name"], v["status"], v["auth"]) == ("my_notes", "My Notes", "ACTIVE", "none")
    assert v["host"].startswith("127.0.0.1:") and "/mcp" not in v["host"]
    assert [(t["name"], t["kind"]) for t in v["tools"]] == [("create_note", "act"), ("get_note", "read")]
    assert v["tool_count"] == 2 and v["error"] is None and v["tools_fetched_at"]


async def test_a_pasted_token_is_sent_sealed_and_never_shown(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="paste-me-123") as (fake, url):
        made = await service.create(a.id, name="Locked", url=url, bearer_token=" paste-me-123 ")
        row = await repo.get(a.id, made.server["id"])
    assert fake.requests[0]["auth"] == "Bearer paste-me-123"
    assert made.server["auth"] == "bearer" and "paste-me-123" not in str(made.server)
    assert row.access_token.startswith("n1:") and sealing.tokens_of(row).access == "paste-me-123"


async def test_a_wrong_token_keeps_nothing(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="right") as (fake, url):
        with pytest.raises(svc.ProbeFailed) as err:
            await service.create(a.id, name="Locked", url=url, bearer_token="wrong")
    assert (err.value.code, err.value.kind) == ("auth", Kind.AUTH)
    assert await repo.list_for(a.id) == []


@pytest.mark.parametrize(("status", "code", "kind"), [
    (404, "not_found", Kind.PERMANENT), (405, "not_mcp", Kind.PERMANENT),
    (503, "server_error", Kind.TRANSIENT)])
async def test_a_server_that_fails_the_probe_is_not_kept(service, two_users, status, code, kind) -> None:
    a, _ = two_users
    async with open_server(always_status=status) as (fake, url):
        with pytest.raises(svc.ProbeFailed) as err:
            await service.create(a.id, name="Broken", url=url)
    assert (err.value.code, err.value.kind) == (code, kind)
    assert err.value.message and await repo.count(a.id) == 0


async def test_nothing_listening_is_unreachable(service, two_users) -> None:
    a, _ = two_users
    with pytest.raises(svc.ProbeFailed) as err:
        await service.create(a.id, name="Gone", url="http://127.0.0.1:9/mcp")
    assert err.value.code == "unreachable"


@pytest.mark.parametrize(("kw", "field"), [
    ({"name": "x", "url": "http://10.0.0.5/mcp"}, "url"),
    ({"name": "x", "url": "https://user:pw@mcp.example.com/mcp"}, "url"),
    ({"name": "x", "url": "https://203.0.113.9/mcp"}, "url"),
    ({"name": "x", "url": ""}, "url"),
    ({"name": "x", "url": "https://mcp.example.com/" + "a" * 2100}, "url"),
    ({"name": "  ", "url": "https://mcp.example.com/mcp"}, "name"),
    ({"name": "x", "url": "https://mcp.example.com/mcp", "bearer_token": "two words"}, "bearer_token"),
    ({"name": "x", "url": "https://mcp.example.com/mcp", "bearer_token": "tok\r\nX-Evil: 1"}, "bearer_token"),
    ({"name": "x", "url": "https://mcp.example.com/mcp", "bearer_token": "a" * 5000}, "bearer_token"),
    ({"catalog_id": "no-such-entry"}, "catalog_id"),
])
async def test_bad_input_is_refused_before_any_request(service, two_users, kw, field) -> None:
    a, _ = two_users
    with pytest.raises(svc.InvalidInput) as err:
        await service.create(a.id, **kw)
    assert err.value.field == field and await repo.count(a.id) == 0


async def test_the_same_address_twice_is_a_duplicate_and_same_names_get_numbered_slugs(service, two_users):
    a, _ = two_users
    async with open_server() as (f1, u1), open_server() as (f2, u2):
        first = await service.create(a.id, name="Notes", url=u1)
        with pytest.raises(svc.Duplicate):
            await service.create(a.id, name="Other", url=u1)
        second = await service.create(a.id, name="Notes", url=u2)
    assert (first.server["slug"], second.server["slug"]) == ("notes", "notes_2")


async def test_ten_servers_is_the_limit(service, two_users) -> None:
    a, b = two_users
    async with open_server() as (fake, url):
        for i in range(10):
            await service.create(a.id, name=f"S{i}", url=f"{url}?n={i}")
        with pytest.raises(svc.LimitReached):
            await service.create(a.id, name="Eleven", url=f"{url}?n=11")
        await service.create(b.id, name="Mine", url=url)  # the limit is per user
    assert await repo.count(a.id) == 10


async def test_a_catalog_server_without_sign_in(service, two_users, monkeypatch) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        entry = catalog.CatalogEntry("fakedocs", "Fake Docs", "Docs for tests.", url, "none")
        monkeypatch.setattr(catalog, "_BY_ID", {"fakedocs": entry})
        made = await service.create(a.id, catalog_id="fakedocs")
    assert made.server["catalog_id"] == "fakedocs" and made.server["slug"] == "fake_docs"
    assert made.server["status"] == "ACTIVE" and made.server["tool_count"] == 2


# --- OAuth ----------------------------------------------------------------------------------------

async def test_a_catalog_oauth_server_goes_through_consent_and_then_works(service, two_users, monkeypatch):
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        monkeypatch.setattr(catalog, "_BY_ID", {"vendor": catalog.CatalogEntry(
            "vendor", "Vendor", "A vendor.", url, "oauth")})
        made = await service.create(a.id, catalog_id="vendor")
        assert made.server["status"] == "NEEDS_AUTH" and made.server["tool_count"] == 0
        assert urlsplit(made.authorize_url).path == "/authorize"
        state, code = consent(auth, made.authorize_url)
        done = await service.complete_oauth(a.id, state, code)
        row = await repo.get(a.id, made.server["id"])
    assert done == svc.Completed(True, a.id, made.server["id"])
    assert (row.status, row.auth_kind, row.tools and len(row.tools)) == ("ACTIVE", "oauth", 2)
    assert sealing.tokens_of(row).access == "access-1" and row.oauth["issuer"] == auth.base
    assert row.oauth["registration"] == "dcr" and row.oauth["client_id"] == "client-1"
    assert fake.requests[-1]["auth"] == "Bearer access-1"


async def test_a_custom_server_that_answers_401_with_oauth_metadata_starts_consent(service, two_users):
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto OAuth", url=url)
        row = await repo.get(a.id, made.server["id"])
    assert made.authorize_url and (row.auth_kind, row.status) == ("oauth", "NEEDS_AUTH")
    assert any(r["path"] == "/register" for r in auth.requests)


async def test_a_server_that_wants_a_token_and_offers_no_oauth_is_not_kept(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="needed") as (fake, url):
        with pytest.raises(svc.ProbeFailed) as err:
            await service.create(a.id, name="Closed", url=url)
    assert (err.value.code, err.value.kind) == ("needs_token", Kind.AUTH)
    assert await repo.count(a.id) == 0


async def test_reconnecting_reuses_the_registered_client(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        again = await service.connect(a.id, made.server["id"])
        regs = [r for r in auth.requests if r["path"] == "/register"]
    assert len(regs) == 1 and again.authorize_url and again.authorize_url != made.authorize_url
    assert (await repo.get(a.id, made.server["id"])).oauth["client_id"] == "client-1"


async def test_the_callback_state_is_single_use_and_bound_to_the_session_user(service, two_users) -> None:
    a, b = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        wrong = await service.complete_oauth(b.id, state, code)  # another user's browser
        assert (wrong.ok, wrong.code) == (False, "session_mismatch")
        replay = await service.complete_oauth(a.id, state, code)  # the state is spent whoever used it
        assert (replay.ok, replay.code) == (False, "bad_state")
        assert (await repo.get(a.id, made.server["id"])).status == "NEEDS_AUTH"
        none = await service.complete_oauth(None, "garbage", "x")
        assert (none.ok, none.code) == (False, "bad_state")
        fresh_state, _ = await _fresh(service, a.id, made.server["id"], auth)
        assert (await service.complete_oauth(None, fresh_state, "x")).code == "session_mismatch"


async def _fresh(service, user_id, server_id, auth):
    link = (await service.connect(user_id, server_id)).authorize_url
    return consent(auth, link)


async def test_a_refused_code_leaves_the_server_needing_sign_in(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, _code = consent(auth, made.authorize_url)
        done = await service.complete_oauth(a.id, state, "not-a-real-code")
        row = await repo.get(a.id, made.server["id"])
    assert (done.ok, done.code) == (False, "oauth_token")
    assert (row.status, row.last_error, row.access_token) == ("NEEDS_AUTH", "oauth_token", None)


async def test_a_callback_from_another_issuer_is_refused_and_the_matching_one_works(service, two_users):
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        bad = await service.complete_oauth(a.id, state, code, iss="https://evil.example.com")
        assert (bad.ok, bad.code) == (False, "issuer_mismatch")
        state2, code2 = await _fresh(service, a.id, made.server["id"], auth)
        good = await service.complete_oauth(a.id, state2, code2, iss=auth.base)
    assert good.ok


async def test_a_missing_issuer_is_refused_when_the_server_promised_one(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        row = await repo.get(a.id, made.server["id"])
        await repo.save_oauth(a.id, row.id, {**row.oauth, "iss_parameter_supported": True})
        state, code = consent(auth, made.authorize_url)
        out = await service.complete_oauth(a.id, state, code)
    assert (out.ok, out.code) == (False, "issuer_mismatch")


async def test_deny_spends_the_state(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        assert await service.deny(state) == a.id
        assert (await service.complete_oauth(a.id, state, code)).code == "bad_state"


async def test_oauth_discovery_failure_deletes_the_new_server(service, two_users, monkeypatch) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        monkeypatch.setattr(catalog, "_BY_ID", {"v": catalog.CatalogEntry("v", "V", "d" * 12, url, "oauth")})
        with pytest.raises(svc.ProbeFailed) as err:
            await service.create(a.id, catalog_id="v")
    assert err.value.code == "oauth_unsupported" and await repo.count(a.id) == 0


async def test_a_changed_issuer_drops_the_old_tokens_so_they_never_meet_the_new_one(service, two_users):
    a, _ = two_users
    other = FakeAuthServer()
    async with oauth_server() as (auth, fake, url), serve(other.app) as other_base:
        other.base = other_base
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        assert (await service.complete_oauth(a.id, state, code)).ok
        fake.prm = {**fake.prm, "authorization_servers": [other_base]}  # the server moved to another issuer
        again = await service.connect(a.id, made.server["id"])
        row = await repo.get(a.id, made.server["id"])
        assert again.authorize_url and row.oauth["issuer"] == other_base
        assert (row.access_token, row.refresh_token, row.expires_at) == (None, None, None)
        await service.refresh_view(a.id, made.server["id"])
    assert not [r for r in other.requests if r["path"] == "/token"]


# --- reconnecting a token server ------------------------------------------------------------------

async def test_a_new_token_reconnects_a_token_server(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="old") as (fake, url):
        made = await service.create(a.id, name="T", url=url, bearer_token="old")
        fake.token = "new"
        assert (await service.refresh_view(a.id, made.server["id"]))["status"] == "NEEDS_AUTH"
        with pytest.raises(svc.InvalidInput):
            await service.connect(a.id, made.server["id"])  # a token is required
        with pytest.raises(svc.ProbeFailed):
            await service.connect(a.id, made.server["id"], bearer_token="still-wrong")
        done = await service.connect(a.id, made.server["id"], bearer_token="new")
    assert done.server["status"] == "ACTIVE" and done.authorize_url is None


async def test_an_open_server_cannot_be_reconnected_with_a_token(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Open", url=url)
        with pytest.raises(svc.InvalidInput):
            await service.connect(a.id, made.server["id"], bearer_token="x")


# --- refresh and status transitions ---------------------------------------------------------------

async def test_refresh_picks_up_new_tools(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Notes", url=url)
        fake.tools.append(tool("list_notes", read_only=True))
        again = await service.refresh_view(a.id, made.server["id"])
    assert again["tool_count"] == 3 and "list_notes" in [t["name"] for t in again["tools"]]


@pytest.mark.parametrize(("status", "expect", "error"), [
    (401, "NEEDS_AUTH", "auth"), (403, "NEEDS_AUTH", "auth"), (404, "ERROR", "not_found"),
    (405, "ERROR", "not_mcp"), (500, "ACTIVE", None), (429, "ACTIVE", None)])
async def test_status_follows_what_a_refresh_finds(service, two_users, status, expect, error) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Notes", url=url)
        fake.always_status = status
        v = await service.refresh_view(a.id, made.server["id"])
        assert (v["status"], v["error"]) == (expect, error)
        fake.always_status = None
        back = await service.refresh_view(a.id, made.server["id"])
    assert (back["status"], back["error"]) == ("ACTIVE", None)  # it recovers on its own when it works again


async def test_an_oauth_401_refreshes_the_token_once_and_tries_again(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        assert (await service.complete_oauth(a.id, state, code)).ok  # holds access-1
        fake.token = "access-2"  # the server no longer accepts access-1, the next one is fine
        v = await service.refresh_view(a.id, made.server["id"])
        row = await repo.get(a.id, made.server["id"])
    assert v["status"] == "ACTIVE" and auth.refresh_calls == 1
    assert sealing.tokens_of(row).access == "access-2"


async def test_the_forced_refresh_after_a_401_carries_the_401s_pointer(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        assert (await service.complete_oauth(a.id, state, code)).ok
        fake.token = "access-2"
        seen, real = [], service.tokens.access_token

        async def spy(*args, **kw):
            seen.append(kw)
            return await real(*args, **kw)

        service.tokens.access_token = spy
        await service.refresh_view(a.id, made.server["id"])
    forced = [kw for kw in seen if kw.get("force")]
    assert len(forced) == 1 and "resource_metadata=" in (forced[0]["www_authenticate"] or "")


async def test_an_oauth_server_whose_refresh_is_refused_needs_sign_in(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        await service.complete_oauth(a.id, state, code)
        fake.token = "never"
        auth.refresh_tokens.clear()
        v = await service.refresh_view(a.id, made.server["id"])
    assert (v["status"], v["error"]) == ("NEEDS_AUTH", "oauth_token")


async def test_stale_caches_are_refreshed_for_a_listing_but_never_for_long(service, two_users, monkeypatch):
    from datetime import timedelta

    from mavis.domain import timeutil

    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Notes", url=url)
        fake.tools.append(tool("list_notes", read_only=True))
        fresh = await service.fresh_servers(a.id)
        assert len(fresh[0].tools) == 2  # still fresh: no refresh
        await repo.save_tools(a.id, made.server["id"], fresh[0].tools, timeutil.now() - timedelta(minutes=16))
        assert len((await service.fresh_servers(a.id))[0].tools) == 3
        await repo.save_tools(a.id, made.server["id"], fresh[0].tools, timeutil.now() - timedelta(minutes=16))
        fake.list_delay_s = 5
        monkeypatch.setattr(svc, "LISTING_REFRESH_S", 0.4)
        slow = await service.fresh_servers(a.id)
        assert len(slow[0].tools) == 2  # too slow: the cache is shown as it was


# --- resolving and checking -----------------------------------------------------------------------

async def test_resolve_finds_a_cached_tool_without_a_refresh(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        before = len(fake.requests)
        got = await service.resolve(a.id, "NOTES", "get_note")
    assert got.tool["kind"] == "read" and got.server.slug == "notes" and len(fake.requests) == before


async def test_resolve_refreshes_once_for_a_tool_it_does_not_know_and_then_finds_it(service, two_users):
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        fake.tools.append(tool("list_notes", read_only=True))
        assert (await service.resolve(a.id, "notes", "list_notes")).tool["name"] == "list_notes"
        with pytest.raises(svc.Refusal) as err:
            await service.resolve(a.id, "notes", "ghost")
    assert "no tool called 'ghost'" in err.value.text and "mcp_tools" in err.value.text


async def test_resolve_names_the_connected_servers_for_an_unknown_one(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        with pytest.raises(svc.Refusal) as err:
            await service.resolve(a.id, "nope", "get_note")
    assert "no connected server called 'nope'" in err.value.text
    assert "Connected servers: notes" in err.value.text


async def test_resolve_tells_the_model_to_stop_when_a_server_needs_attention(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Notes", url=url)
        await repo.set_status(a.id, made.server["id"], repo.NEEDS_AUTH, "auth")
        with pytest.raises(svc.Refusal) as err:
            await service.resolve(a.id, "notes", "get_note")
        await repo.set_status(a.id, made.server["id"], repo.ERROR, "not_found")
        with pytest.raises(svc.Refusal) as err2:
            await service.resolve(a.id, "notes", "get_note")
    assert "needs to be reconnected in the dashboard; do not retry" in err.value.text
    assert "cannot be used right now" in err2.value.text and "do not retry" in err2.value.text


async def test_check_validates_arguments_and_drops_model_nulls(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        got = await service.resolve(a.id, "notes", "create_note")
    cleaned, problem = service.check(got, {"title": "x", "note_color": None})
    assert cleaned == {"title": "x"} and problem is None
    assert service.check(got, {})[1] == "Invalid arguments: 'title' is a required property"
    assert "is not of type 'string'" in service.check(got, {"title": 5})[1]


# --- calling --------------------------------------------------------------------------------------

async def test_a_read_call_returns_the_rendered_result(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        got = await service.resolve(a.id, "notes", "get_note")
        out = await service.call(got, {"id": "7"}, retry_transient=True)
    assert out == svc.CallResult("note 7", ok=True)
    assert fake.calls == [("get_note", {"id": "7"})]


async def test_a_tool_reported_error_is_a_completed_call(service, two_users) -> None:
    a, _ = two_users
    broken = {"get_note": lambda a: text_result("no such note", is_error=True)}
    async with open_server(handlers=broken) as (f, u):
        await service.create(a.id, name="Notes", url=u)
        out = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                 retry_transient=True)
    assert (out.ok, out.tool_error, out.text) == (True, True, "no such note")


async def test_a_read_is_retried_once_on_a_transient_failure(service, two_users, monkeypatch) -> None:
    a, _ = two_users
    async with open_server(fail_next_calls=1) as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        out = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                 retry_transient=True)
        assert (out.ok, out.text) == (True, "note 1") and fake.attempts == 2
        fake.fail_next_calls = 5
        out2 = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                  retry_transient=True)
    assert out2.ok is False and out2.code == "server_error" and fake.attempts == 4  # one retry, then it stops
    assert "may work later" in out2.text


async def test_an_act_is_never_replayed_and_reports_an_unknown_outcome(service, two_users) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        fake.drop_calls = True
        out = await service.call(await service.resolve(a.id, "notes", "create_note"), {"title": "x"},
                                 retry_transient=False)
    assert fake.attempts == 1 and out.ok is False and out.unknown_outcome is True
    assert "outcome is unknown" in out.text and "do not retry it automatically" in out.text
    assert "Notes" in out.text and "create_note" in out.text


async def test_a_503_on_an_act_is_also_unknown_and_not_retried(service, two_users) -> None:
    a, _ = two_users
    async with open_server(fail_next_calls=5) as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        out = await service.call(await service.resolve(a.id, "notes", "create_note"), {"title": "x"},
                                 retry_transient=False)
    assert fake.attempts == 1 and out.unknown_outcome is True


async def test_a_401_during_a_call_marks_the_server_and_tells_the_model_to_stop(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="good") as (fake, url):
        made = await service.create(a.id, name="Notes", url=url, bearer_token="good")
        got = await service.resolve(a.id, "notes", "get_note")
        fake.token = "rotated"
        out = await service.call(got, {"id": "1"}, retry_transient=True)
        row = await repo.get(a.id, made.server["id"])
    assert out.ok is False and out.code == "auth"
    assert "needs to be reconnected in the dashboard; do not retry" in out.text
    assert (row.status, row.last_error) == ("NEEDS_AUTH", "auth")


async def test_a_401_on_an_oauth_act_refreshes_the_token_and_runs_it_once(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        await service.complete_oauth(a.id, state, code)
        got = await service.resolve(a.id, "auto", "create_note")
        fake.token = "access-2"
        out = await service.call(got, {"title": "t"}, retry_transient=False)
    assert out == svc.CallResult("made t", ok=True) and fake.calls == [("create_note", {"title": "t"})]


async def test_a_refreshed_token_is_redacted_from_the_result(service, two_users) -> None:
    a, _ = two_users
    echo = {"get_note": lambda args: text_result("the token is access-2 ok", is_error=True)}
    async with oauth_server() as (auth, fake, url):
        fake.handlers = echo
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        await service.complete_oauth(a.id, state, code)
        got = await service.resolve(a.id, "auto", "get_note")
        fake.token = "access-2"  # the call refreshes mid-way; the stale snapshot knows only access-1
        out = await service.call(got, {"id": "1"}, retry_transient=True)
    assert out.ok and "access-2" not in out.text and "[REDACTED]" in out.text


@pytest.mark.parametrize("failure", [
    Failure(Kind.TRANSIENT, "connection_lost"), Failure(Kind.PERMANENT, "redirect"),
    Failure(Kind.PERMANENT, "protocol")])
async def test_an_act_that_may_have_reached_the_server_says_the_outcome_is_unknown(
        service, two_users, monkeypatch, failure) -> None:
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        got = await service.resolve(a.id, "notes", "create_note")

        async def boom(*args, **kwargs):
            raise McpFailure(failure)

        monkeypatch.setattr(service.client, "call_tool", boom)
        out = await service.call(got, {"title": "x"}, retry_transient=False)
    assert out.ok is False and out.unknown_outcome is True
    assert "outcome is unknown" in out.text and "cannot be used right now" not in out.text


async def test_a_refreshed_token_is_redacted_from_a_successful_result_too(service, two_users) -> None:
    a, _ = two_users
    async with oauth_server() as (auth, fake, url):
        fake.handlers = {"get_note": lambda args: text_result("the token is access-2 ok")}
        made = await service.create(a.id, name="Auto", url=url)
        state, code = consent(auth, made.authorize_url)
        await service.complete_oauth(a.id, state, code)
        got = await service.resolve(a.id, "auto", "get_note")
        fake.token = "access-2"
        out = await service.call(got, {"id": "1"}, retry_transient=True)
    assert out.ok and not out.tool_error and "access-2" not in out.text and "[REDACTED]" in out.text


@pytest.mark.parametrize(("code", "unknown"), [
    (-32602, False), (-32601, False), (-32603, True), (-32000, True), (-32001, True), (12345, True)])
async def test_only_a_server_refusal_proves_an_act_did_not_run(
        service, two_users, monkeypatch, code, unknown) -> None:
    from mavis.mcp import failures
    from mcp import MCPError

    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        got = await service.resolve(a.id, "notes", "create_note")

        async def boom(*args, **kwargs):
            raise McpFailure(failures.classify(MCPError(code, "x"), phase="call"))

        monkeypatch.setattr(service.client, "call_tool", boom)
        out = await service.call(got, {"title": "x"}, retry_transient=False)
    assert out.ok is False and out.unknown_outcome is unknown
    assert ("outcome is unknown" in out.text) is unknown


async def test_secrets_never_reach_the_model_through_a_result(service, two_users) -> None:
    a, _ = two_users
    said = "auth failed for Bearer good-token-1 key=zzz98765"
    leak = {"get_note": lambda args: text_result(said, is_error=True)}
    async with open_server(token="good-token-1", handlers=leak) as (fake, url):
        await service.create(a.id, name="Notes", url=url, bearer_token="good-token-1")
        out = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                 retry_transient=True)
    assert "good-token-1" not in out.text and "zzz98765" not in out.text and "[REDACTED]" in out.text


async def test_a_servers_own_reason_for_refusing_a_call_reaches_the_model_sanitized(service, two_users):
    a, _ = two_users
    async with open_server() as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        fake.handlers.pop("get_note")  # the server now answers "Unknown tool: get_note" (a JSON-RPC error)
        out = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                 retry_transient=True)
    assert out.ok is False and out.code == "rpc_error"
    assert "The server said: MCPError: Unknown tool: get_note" in out.text


async def test_failure_text_never_carries_a_response_body(service, two_users) -> None:
    a, _ = two_users
    async with open_server(fail_next_calls=9) as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        out = await service.call(await service.resolve(a.id, "notes", "get_note"), {"id": "1"},
                                 retry_transient=True)
    assert "abc123" not in out.text and "busy" not in out.text


async def test_sessions_are_capped_process_wide(mcp_env, two_users, monkeypatch) -> None:
    a, _ = two_users
    service = McpService()
    service._slots = asyncio.Semaphore(1)
    async with open_server(delay_s=0.3) as (fake, url):
        await service.create(a.id, name="Notes", url=url)
        got = await service.resolve(a.id, "notes", "get_note")
        await asyncio.gather(*(service.call(got, {"id": str(i)}, retry_transient=True) for i in range(3)))
    assert fake.max_in_flight == 1


# --- isolation ------------------------------------------------------------------------------------

async def test_one_user_can_never_reach_anothers_server(service, two_users) -> None:
    a, b = two_users
    async with open_server() as (fake, url):
        made = await service.create(a.id, name="Notes", url=url)
        sid = made.server["id"]
        assert await service.list_servers(b.id) == []
        with pytest.raises(svc.Refusal):
            await service.resolve(b.id, "notes", "get_note")
        with pytest.raises(svc.NotFound):
            await service.get_view(b.id, sid)
        with pytest.raises(svc.NotFound):
            await service.refresh_tools(b.id, sid)
        with pytest.raises(svc.NotFound):
            await service.connect(b.id, sid)
        assert await service.delete(b.id, sid) is False
        assert await service.has_active(b.id) is False and await service.has_active(a.id) is True
        assert fake.attempts == 0
        # B can use the same slug for B's own server and A's stays A's
        other = await service.create(b.id, name="Notes", url=url)
        assert other.server["id"] != sid and (await service.get_view(a.id, sid))["slug"] == "notes"


async def test_deleting_removes_the_server_and_its_secrets(service, two_users) -> None:
    a, _ = two_users
    async with open_server(token="tok-1") as (fake, url):
        made = await service.create(a.id, name="Notes", url=url, bearer_token="tok-1")
    assert await service.delete(a.id, made.server["id"]) is True
    assert await repo.list_for(a.id) == [] and await service.delete(a.id, made.server["id"]) is False


# --- the words ------------------------------------------------------------------------------------

def test_no_message_has_an_em_or_en_dash() -> None:
    from mavis.mcp import messages

    for text in messages.USER_TEXT.values():
        assert "—" not in text and "–" not in text
    for kind in Kind:
        assert "—" not in messages.model_text("X", "unknown", kind, tool="t")
    assert "—" not in messages.model_text("X", "timeout", Kind.TRANSIENT, unknown_outcome=True, tool="t")
