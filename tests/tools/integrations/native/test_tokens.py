import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.store import db as dbm
from mavis.store.models import NativeGrant
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL, SLACK_TOKEN_URL

from .conftest import form

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK


async def save(tokens, provider=G, *, expires_in=3600, refresh="r-1", access="a-1", user=1):
    exp = timeutil.now() + timedelta(seconds=expires_in) if expires_in is not None else None
    await tokens.save(user, provider, account={"email": "a@x.com", "scopes": ["openid"]},
                      access_token=access, refresh_token=refresh, expires_at=exp)


def google_ok(counter=None, token="a-2"):
    def handler(request):
        if counter is not None:
            counter.append(form(request))
        return httpx.Response(200, json={"access_token": token, "expires_in": 3600, "token_type": "Bearer"})
    return handler


async def test_fresh_token_is_returned_without_a_vendor_call(tokens, vendor):
    await save(tokens)
    assert await tokens.access_token(1, G) == "a-1"
    assert vendor.requests == []


async def test_tokens_are_sealed_at_rest(tokens):
    await save(tokens, access="ya29.plain", refresh="1//plainrefresh")
    async with dbm.Session() as s:
        row = await s.scalar(select(NativeGrant))
    assert "plain" not in row.access_token and "plain" not in (row.refresh_token or "")
    assert "plain" not in str(row.account)
    assert await tokens.account(1, G) == {"email": "a@x.com", "scopes": ["openid"]}


@pytest.mark.parametrize("left_s", [60, 299, 0, -100])
async def test_refreshes_inside_the_five_minute_window(tokens, vendor, left_s):
    await save(tokens, expires_in=left_s)
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    assert await tokens.access_token(1, G) == "a-2"
    [sent] = calls
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "r-1"
    assert sent["client_id"] == "gid" and sent["client_secret"] == "gsecret"
    assert await tokens.access_token(1, G) == "a-2" and len(calls) == 1  # saved: no second refresh
    grant = await tokens.grant(1, G)
    assert grant.expires_at > timeutil.now() + timedelta(minutes=50)


async def test_not_refreshed_just_outside_the_window(tokens, vendor):
    await save(tokens, expires_in=301)
    assert await tokens.access_token(1, G) == "a-1" and vendor.requests == []


async def test_google_keeps_its_refresh_token_when_none_is_returned(tokens, vendor):
    await save(tokens, expires_in=10)
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    await tokens.access_token(1, G)
    assert (await tokens.reveal(1, G)) == ("a-2", "r-1")


async def test_concurrent_callers_share_one_refresh(tokens, vendor):
    await save(tokens, expires_in=10)
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    got = await asyncio.gather(*(tokens.access_token(1, G) for _ in range(12)))
    assert set(got) == {"a-2"} and len(calls) == 1


async def test_concurrent_forced_refreshes_after_a_401_share_one_refresh(tokens, vendor):
    await save(tokens, expires_in=3000)
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    got = await asyncio.gather(*(tokens.access_token(1, G, force=True) for _ in range(8)))
    assert set(got) == {"a-2"} and len(calls) == 1


async def test_force_after_401_refreshes_even_when_not_expired(tokens, vendor):
    await save(tokens, expires_in=3000)
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    assert await tokens.access_token(1, G) == "a-1" and calls == []
    assert await tokens.access_token(1, G, force=True) == "a-2" and len(calls) == 1
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls, token="a-3")
    assert await tokens.access_token(1, G, force=True) == "a-3"  # a later 401 refreshes again


async def test_different_users_and_providers_do_not_block_each_other(tokens, vendor):
    await save(tokens, expires_in=10, user=1)
    await save(tokens, expires_in=10, user=2, access="b-1", refresh="r-b")
    calls = []
    vendor.routes[GOOGLE_TOKEN_URL] = google_ok(calls)
    await asyncio.gather(tokens.access_token(1, G), tokens.access_token(2, G))
    assert sorted(c["refresh_token"] for c in calls) == ["r-1", "r-b"]


@pytest.mark.parametrize("status,body", [(400, {"error": "invalid_grant"}),
                                         (401, {"error": "invalid_grant", "error_description": "Bad"})])
async def test_invalid_grant_marks_revoked_and_requires_reauth(tokens, vendor, status, body):
    await save(tokens, expires_in=10)
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(status, json=body)
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, G)
    assert (await tokens.grant(1, G)).status == "REVOKED"
    assert await tokens.account(1, G) is None
    vendor.requests.clear()
    with pytest.raises(ReauthRequired):  # stays revoked without hitting the vendor again
        await tokens.access_token(1, G)
    assert vendor.requests == []


@pytest.mark.parametrize("error", ["token_revoked", "account_inactive", "invalid_refresh_token"])
async def test_slack_gone_errors_mark_revoked(tokens, vendor, error):
    await save(tokens, S, expires_in=10)
    vendor.routes[SLACK_TOKEN_URL] = lambda r: httpx.Response(200, json={"ok": False, "error": error})
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, S)
    assert (await tokens.grant(1, S)).status == "REVOKED"


async def test_slack_rotation_stores_the_new_refresh_token(tokens, vendor):
    await save(tokens, S, expires_in=10, access="xoxe-1", refresh="xoxe-r1")
    calls = []

    def handler(r):
        calls.append(form(r))
        return httpx.Response(200, json={"ok": True, "access_token": "xoxe-2", "refresh_token": "xoxe-r2",
                                         "expires_in": 43200})
    vendor.routes[SLACK_TOKEN_URL] = handler
    assert await tokens.access_token(1, S) == "xoxe-2"
    assert calls[0]["refresh_token"] == "xoxe-r1" and calls[0]["client_id"] == "sid"
    assert await tokens.reveal(1, S) == ("xoxe-2", "xoxe-r2")


async def test_long_lived_slack_token_never_refreshes(tokens, vendor):
    await save(tokens, S, expires_in=None, refresh=None, access="xoxp-1")
    assert await tokens.access_token(1, S) == "xoxp-1" and vendor.requests == []


async def test_force_without_refresh_token_means_reconnect(tokens, vendor):
    await save(tokens, S, expires_in=None, refresh=None, access="xoxp-1")
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, S, force=True)
    assert (await tokens.grant(1, S)).status == "REVOKED"
    assert vendor.requests == []


async def test_expired_google_token_without_refresh_token_requires_reauth(tokens):
    await save(tokens, expires_in=-5, refresh=None)
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, G)


@pytest.mark.parametrize("response", [
    lambda r: httpx.Response(500, text="boom secret-body"),
    lambda r: httpx.Response(503, json={"error": "backend_error"}),
    lambda r: httpx.Response(400, json={"error": "invalid_client"}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
async def test_transient_or_config_failures_do_not_revoke(tokens, vendor, response):
    await save(tokens, expires_in=10)
    vendor.routes[GOOGLE_TOKEN_URL] = response
    with pytest.raises(IntegrationError) as exc:
        await tokens.access_token(1, G)
    assert "secret-body" not in str(exc.value) and "r-1" not in str(exc.value)
    assert (await tokens.grant(1, G)).status == "ACTIVE"


async def test_not_connected_requires_reauth(tokens):
    with pytest.raises(ReauthRequired):
        await tokens.access_token(9, G)
    assert await tokens.account(9, G) is None


async def test_reconnect_revives_a_revoked_grant(tokens, vendor):
    await save(tokens, expires_in=10)
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(400, json={"error": "invalid_grant"})
    with pytest.raises(ReauthRequired):
        await tokens.access_token(1, G)
    await save(tokens, access="a-9", refresh="r-9")
    assert await tokens.access_token(1, G) == "a-9"
    assert len(await tokens.grants(1)) == 1


async def test_owner_of_finds_the_holder_of_a_vendor_account(tokens):
    await save(tokens)
    assert await tokens.owner_of(G, "email", "a@x.com") == 1
    assert await tokens.owner_of(G, "email", "other@x.com") is None
    assert await tokens.owner_of(S, "email", "a@x.com") is None
