"""Access tokens for a server row: bearer, OAuth, the 60 second rule and the refresh fence."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mavis.domain import timeutil
from mavis.mcp import oauth as o
from mavis.mcp import sealing
from mavis.mcp.failures import Kind, McpFailure
from mavis.mcp.tokens import TokenProvider
from mavis.store.repo import mcp_servers as repo
from tests.mcp.test_oauth import stack


async def oauth_row(user_id, auth, url, *, expires_in_s: int | None = 3600, refresh: str | None = "refresh-1",
                    slug="linear"):
    """A connected OAuth server row whose facts come from real discovery against the fake servers."""
    facts = (await o.McpOAuth().discover(url)).facts(
        "client-1", sealing.seal("csecret", user_id, 0, sealing.CLIENT_SECRET), registration="dcr")
    row = await repo.create(user_id, slug=slug, display_name="Linear", url=url, auth_kind="oauth")
    facts["client_secret"] = sealing.seal("csecret", user_id, row.id, sealing.CLIENT_SECRET)
    await repo.save_oauth(user_id, row.id, facts)
    expires = None if expires_in_s is None else timeutil.now() + timedelta(seconds=expires_in_s)
    await repo.save_tokens(user_id, row.id, access="stored-access", refresh=refresh, expires_at=expires)
    return row


def provider() -> TokenProvider:
    return TokenProvider(o.McpOAuth())


async def test_no_token_for_an_open_server_and_the_pasted_one_for_a_bearer_server(mcp_env, two_users):
    a, _ = two_users
    none = await repo.create(a.id, slug="open", display_name="Open", url="https://x.example.com/mcp",
                             auth_kind="none")
    bearer = await repo.create(a.id, slug="paste", display_name="Paste", url="https://y.example.com/mcp",
                               auth_kind="bearer", bearer="pasted-token-1")
    assert await provider().access_token(a.id, none.id) is None
    assert await provider().access_token(a.id, bearer.id) == "pasted-token-1"


async def test_a_fresh_oauth_token_is_returned_without_any_request(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url)
        before = len(auth.requests)
        assert await provider().access_token(a.id, row.id) == "stored-access"
        assert len(auth.requests) == before


async def test_a_token_without_an_expiry_is_used_as_is(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=None)
        assert await provider().access_token(a.id, row.id) == "stored-access"
        assert auth.refresh_calls == 0


@pytest.mark.parametrize("seconds", [59, 30, 0, -3600])
async def test_a_token_within_sixty_seconds_of_expiry_is_refreshed(mcp_env, two_users, seconds) -> None:
    a, _ = two_users
    async with stack(rotate_refresh=True) as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=seconds)
        assert await provider().access_token(a.id, row.id) == "access-1"
        stored = await repo.get(a.id, row.id)
        t = sealing.tokens_of(stored)
        assert (t.access, t.refresh) == ("access-1", "refresh-2")
        assert stored.status == repo.ACTIVE and stored.expires_at > timeutil.now() + timedelta(minutes=30)
        assert "access-1" not in stored.access_token  # sealed at rest


async def test_a_token_a_minute_and_a_bit_from_expiry_is_still_fresh(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=75)
        assert await provider().access_token(a.id, row.id) == "stored-access"


async def test_the_refresh_token_is_kept_when_the_server_does_not_rotate_it(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack(rotate_refresh=False) as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        await provider().access_token(a.id, row.id)
        assert sealing.tokens_of(await repo.get(a.id, row.id)).refresh == "refresh-1"


async def test_force_refreshes_a_fresh_token_after_a_401(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url)
        assert await provider().access_token(a.id, row.id, force=True) == "access-1"
        assert auth.refresh_calls == 1


async def test_concurrent_callers_share_one_refresh(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack(rotate_refresh=True, refresh_delay_s=0.3) as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        shared = provider()
        got = await asyncio.gather(*(shared.access_token(a.id, row.id) for _ in range(6)))
    assert auth.refresh_calls == 1 and set(got) == {"access-1"}


async def test_force_after_another_caller_refreshed_does_not_refresh_again(mcp_env, two_users) -> None:
    """Six callers got a 401 with the same old token and all force a refresh: the first one refreshes; the
    rest find the fresh token already stored."""
    a, _ = two_users
    async with stack(rotate_refresh=True, refresh_delay_s=0.3) as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url)
        shared = provider()
        got = await asyncio.gather(*(shared.access_token(a.id, row.id, force=True) for _ in range(6)))
    assert auth.refresh_calls == 1 and set(got) == {"access-1"}


async def test_a_refused_refresh_sets_needs_auth_and_says_reconnect(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0, refresh="revoked")
        with pytest.raises(McpFailure) as err:
            await provider().access_token(a.id, row.id)
        stored = await repo.get(a.id, row.id)
    assert err.value.kind is Kind.AUTH
    assert (stored.status, stored.last_error) == (repo.NEEDS_AUTH, "oauth_token")


async def test_no_refresh_token_sets_needs_auth(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0, refresh=None)
        with pytest.raises(McpFailure) as err:
            await provider().access_token(a.id, row.id)
        assert err.value.kind is Kind.AUTH
        assert (await repo.get(a.id, row.id)).status == repo.NEEDS_AUTH
        assert auth.refresh_calls == 0


async def test_a_changed_issuer_sets_needs_auth_and_spends_no_refresh_token(mcp_env, two_users) -> None:
    from tests.mcp.fake_auth import FakeAuthServer
    from tests.mcp.localserver import serve

    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        other = FakeAuthServer()
        async with serve(other.app) as other_base:
            other.base = other_base
            mcp.prm["authorization_servers"] = [other_base]
            with pytest.raises(McpFailure) as err:
                await provider().access_token(a.id, row.id)
    assert err.value.code == "issuer_changed"
    assert (await repo.get(a.id, row.id)).last_error == "issuer_changed"
    assert auth.refresh_calls == 0


async def test_a_transient_refresh_failure_leaves_the_server_active(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        auth.token_status = 503
        with pytest.raises(McpFailure) as err:
            await provider().access_token(a.id, row.id)
        stored = await repo.get(a.id, row.id)
    assert err.value.kind is Kind.TRANSIENT and stored.status == repo.ACTIVE


async def test_another_users_server_id_finds_nothing(mcp_env, two_users) -> None:
    a, b = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        with pytest.raises(McpFailure) as err:
            await provider().access_token(b.id, row.id)
        assert err.value.code == "not_found"
        assert auth.refresh_calls == 0


async def test_a_caller_cancelled_mid_refresh_still_saves_the_new_refresh_token(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack(rotate_refresh=True, refresh_delay_s=0.6) as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url, expires_in_s=0)
        shared = provider()
        caller = asyncio.create_task(shared.access_token(a.id, row.id))
        await asyncio.sleep(0.25)  # inside the token POST: the provider has the old refresh token
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        for _ in range(100):
            t = sealing.tokens_of(await repo.get(a.id, row.id))
            if t.refresh == "refresh-2":
                break
            await asyncio.sleep(0.05)
        stored = await repo.get(a.id, row.id)
    assert (t.access, t.refresh) == ("access-1", "refresh-2") and stored.status == repo.ACTIVE
    assert auth.refresh_calls == 1


async def test_a_forced_refresh_passes_the_401s_challenge_to_discovery(mcp_env, two_users) -> None:
    a, _ = two_users
    async with stack() as (auth, mcp, url):
        row = await oauth_row(a.id, auth, url)
        shared, seen = provider(), {}
        real = shared.oauth.refresh

        async def spy(*args, **kw):
            seen.update(kw)
            return await real(*args, **kw)

        shared.oauth.refresh = spy
        challenge = f'Bearer resource_metadata="{mcp.resource_metadata}"'
        await shared.access_token(a.id, row.id, force=True, www_authenticate=challenge)
    assert seen["www_authenticate"] == challenge
