import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.oauth import (
    GOOGLE_REVOKE_URL,
    GOOGLE_USERINFO_URL,
    SLACK_REVOKE_URL,
    OAuthError,
    sign_state,
    verify_state,
)
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL, SLACK_TOKEN_URL

from .conftest import form

G, S = NativeProvider.GOOGLE, NativeProvider.SLACK


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def google_vendor(vendor, *, refresh="1//r", email="Me@Kripya.com", scope="openid email gmail.readonly"):
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(200, json={
        "access_token": "ya29.a", "refresh_token": refresh, "expires_in": 3599, "scope": scope})
    vendor.routes[GOOGLE_USERINFO_URL] = lambda r: httpx.Response(200, json={"sub": "42", "email": email})


def slack_vendor(vendor, **authed):
    body = {"ok": True, "team": {"id": "T1", "name": "Kripya"},
            "authed_user": {"id": "U1", "scope": "channels:history,chat:write", "access_token": "xoxp-1",
                            "token_type": "user", **authed}}
    vendor.routes[SLACK_TOKEN_URL] = lambda r: httpx.Response(200, json=body)


async def test_google_authorize_url(oauth):
    url = await oauth.authorize_url(1, G, 7)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = query(url)
    assert q["client_id"] == "gid" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://mavis.test/oauth/google/callback"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["include_granted_scopes"] == "true" and q["code_challenge_method"] == "S256"
    scopes = q["scope"].split()
    assert {"openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/contacts.readonly"} == set(scopes)
    verifier = verify_state(q["state"]).verifier
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == challenge
    assert verifier not in url  # the verifier travels sealed, never in the clear


async def test_slack_authorize_url_asks_for_user_and_bot_scopes(oauth):
    q = query(await oauth.authorize_url(1, S))
    assert q["client_id"] == "sid"
    assert {"chat:write", "im:history", "im:write", "app_mentions:read", "reactions:write"} <= set(
        q["scope"].split(","))
    assert q["user_scope"].split(",")[0] == "channels:history" and "chat:write" in q["user_scope"]
    assert q["redirect_uri"] == "https://mavis.test/oauth/slack/callback"


async def test_unconfigured_provider_cannot_start(oauth, monkeypatch):
    from mavis.config import get_settings
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "")
    get_settings.cache_clear()
    with pytest.raises(OAuthError) as exc:
        await oauth.authorize_url(1, S)
    assert exc.value.kind == "not_configured"


async def test_google_exchange_saves_a_sealed_grant(oauth, tokens, vendor):
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G, 11))["state"]
    done = await oauth.complete(state, "the-code", G)
    assert (done.user_id, done.provider, done.pending_id) == (5, G, 11)
    sent = form(vendor.to(GOOGLE_TOKEN_URL)[0])
    assert sent["code"] == "the-code" and sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"] == verify_state(state).verifier
    assert sent["redirect_uri"] == "https://mavis.test/oauth/google/callback"
    assert vendor.to(GOOGLE_USERINFO_URL)[0].headers["authorization"] == "Bearer ya29.a"
    assert done.account["email"] == "me@kripya.com"
    assert "openid" in done.account["scopes"]
    assert await tokens.reveal(5, G) == ("ya29.a", "1//r")
    assert (await tokens.grant(5, G)).status == "ACTIVE"


async def test_slack_exchange_stores_team_and_user(oauth, tokens, vendor):
    slack_vendor(vendor)
    state = query(await oauth.authorize_url(5, S, None))["state"]
    done = await oauth.complete(state, "c0de", S)
    assert done.pending_id is None
    assert done.account == {"team_id": "T1", "team_name": "Kripya", "user_id": "U1",
                            "scopes": ["channels:history", "chat:write"]}
    assert "code_verifier" not in form(vendor.to(SLACK_TOKEN_URL)[0])
    assert await tokens.reveal(5, S) == ("xoxp-1", None)
    assert (await tokens.grant(5, S)).expires_at is None  # long-lived xoxp


async def test_slack_rotating_token_keeps_refresh_and_expiry(oauth, tokens, vendor):
    slack_vendor(vendor, refresh_token="xoxe-r", expires_in=43200)
    await oauth.complete(query(await oauth.authorize_url(5, S))["state"], "c", S)
    assert (await tokens.reveal(5, S))[1] == "xoxe-r"
    assert (await tokens.grant(5, S)).expires_at is not None


@pytest.mark.parametrize("token_resp", [
    httpx.Response(400, json={"error": "invalid_grant", "error_description": "leaky vendor text"}),
    httpx.Response(500, text="oops"),
    httpx.Response(200, json={"no": "token"}),
    httpx.Response(200, text="not json"),
])
async def test_google_exchange_failures_never_echo_vendor_text(oauth, tokens, vendor, token_resp):
    vendor.routes[GOOGLE_TOKEN_URL] = lambda r: token_resp
    state = query(await oauth.authorize_url(5, G, 3))["state"]
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(state, "c", G)
    assert exc.value.kind == "exchange_failed" and (exc.value.user_id, exc.value.pending_id) == (5, 3)
    assert "leaky" not in str(exc.value) and await tokens.grant(5, G) is None


async def test_slack_ok_false_is_a_failed_exchange(oauth, vendor):
    vendor.routes[SLACK_TOKEN_URL] = lambda r: httpx.Response(200, json={"ok": False, "error": "bad_code"})
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(query(await oauth.authorize_url(5, S))["state"], "c", S)
    assert exc.value.kind == "exchange_failed" and "bad_code" not in str(exc.value)


async def test_google_without_email_fails(oauth, vendor):
    google_vendor(vendor)
    vendor.routes[GOOGLE_USERINFO_URL] = lambda r: httpx.Response(200, json={"sub": "1"})
    with pytest.raises(OAuthError):
        await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)


async def test_state_is_single_use(oauth, vendor):
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G))["state"]
    await oauth.complete(state, "c", G)
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(state, "c", G)
    assert exc.value.kind == "replayed_state"
    assert len(vendor.to(GOOGLE_TOKEN_URL)) == 1  # the replay never reached the vendor


async def test_state_not_issued_here_is_refused(oauth, vendor):
    forged = sign_state(5, "google", "verifier", nonce="never-stored")
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(forged, "c", G)
    assert exc.value.kind == "replayed_state" and vendor.requests == []


@pytest.mark.parametrize("mangle", [
    lambda s: s[:-3] + ("AAA" if not s.endswith("AAA") else "BBB"),  # bad MAC
    lambda s: s.split(".")[0],
    lambda s: "",
    lambda s: "a.b.c",
    lambda s: "x." + s.split(".")[1],
])
async def test_tampered_state_is_refused(oauth, vendor, mangle):
    state = query(await oauth.authorize_url(5, G))["state"]
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(mangle(state), "c", G)
    assert exc.value.kind == "bad_state" and vendor.requests == []


async def test_state_for_another_provider_is_refused(oauth, vendor):
    state = query(await oauth.authorize_url(5, S))["state"]
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(state, "c", G)
    assert exc.value.kind == "wrong_provider"


async def test_expired_state_is_refused_and_names_the_user(oauth, vendor):
    expired = sign_state(5, "google", "v", nonce="n", ttl_s=-1)
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(expired, "c", G)
    assert exc.value.kind == "expired_state" and exc.value.user_id == 5


async def test_state_expires_in_the_store_too(oauth, vendor, clock):
    google_vendor(vendor)
    state = query(await oauth.authorize_url(5, G))["state"]
    clock.advance(minutes=11)
    with pytest.raises(OAuthError) as exc:  # the store row is past its ttl even if the MAC window moved
        await oauth._consume(verify_state(state), G)
    assert exc.value.kind == "replayed_state"


async def test_state_signed_with_another_key_is_refused(oauth, native_env, monkeypatch):
    from mavis.config import get_settings

    from .conftest import new_kek
    state = sign_state(5, "google", "v", nonce="n")
    monkeypatch.setenv("NATIVE_TOKEN_KEK", new_kek())
    get_settings.cache_clear()
    with pytest.raises(OAuthError) as exc:
        verify_state(state)
    assert exc.value.kind == "bad_state"


async def test_state_expiry_uses_wall_time(native_env):
    ok = sign_state(1, "google", "v", nonce="n", ttl_s=5)
    assert verify_state(ok).exp >= int(time.time())


async def test_denied_spends_the_state_and_names_the_user(oauth):
    state = query(await oauth.authorize_url(5, G, 9))["state"]
    assert await oauth.deny(state, G) == (5, 9)
    with pytest.raises(OAuthError):
        await oauth.deny(state, G)


async def test_one_vendor_account_cannot_serve_two_users(oauth, tokens, vendor):
    google_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(query(await oauth.authorize_url(6, G, 2))["state"], "c", G)
    assert exc.value.kind == "account_taken" and exc.value.user_id == 6
    assert await tokens.grant(6, G) is None
    # the same user reconnecting is fine
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)


async def test_google_revoke_calls_vendor_and_deletes(oauth, tokens, vendor):
    google_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    vendor.routes[GOOGLE_REVOKE_URL] = lambda r: httpx.Response(200, json={})
    await oauth.revoke(5, G)
    assert form(vendor.to(GOOGLE_REVOKE_URL)[0]) == {"token": "1//r"}  # the refresh token kills the grant
    assert await tokens.grant(5, G) is None


async def test_slack_revoke_uses_the_user_token(oauth, tokens, vendor):
    slack_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, S))["state"], "c", S)
    vendor.routes[SLACK_REVOKE_URL] = lambda r: httpx.Response(200, json={"ok": True, "revoked": True})
    await oauth.revoke(5, S)
    assert vendor.to(SLACK_REVOKE_URL)[0].headers["authorization"] == "Bearer xoxp-1"
    assert await tokens.grant(5, S) is None


async def test_revoke_forgets_locally_even_when_the_vendor_fails(oauth, tokens, vendor):
    google_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    vendor.routes[GOOGLE_REVOKE_URL] = lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))
    await oauth.revoke(5, G)
    assert await tokens.grant(5, G) is None
    await oauth.revoke(5, G)  # nothing to revoke is not an error


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ReadError("cut"),
                                 httpx.RemoteProtocolError("cut"), httpx.ConnectError("down")],
                         ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("provider", [G, S], ids=["google", "slack"])
async def test_code_exchange_is_sent_once_whatever_goes_wrong(oauth, vendor, exc, provider):
    """An authorization code is single use: a repeat after the request was sent would fail or burn it."""
    url = GOOGLE_TOKEN_URL if provider is G else SLACK_TOKEN_URL
    vendor.routes[url] = lambda r: (_ for _ in ()).throw(exc)
    state = query(await oauth.authorize_url(5, provider))["state"]
    with pytest.raises(OAuthError) as e:
        await oauth.complete(state, "c", provider)
    assert e.value.kind == "exchange_failed" and len(vendor.to(url)) == 1


# ---- one vendor account, one Mavis user: enforced by the database, not by a check-then-save ------------

async def test_the_database_refuses_a_second_user_for_the_same_account_even_if_the_check_is_skipped(
        oauth, tokens, vendor, monkeypatch):
    google_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)

    async def blind(*a, **k):  # the race: both callbacks passed the owner check before either saved
        return None

    monkeypatch.setattr(tokens, "owner_of", blind)
    with pytest.raises(OAuthError) as exc:
        await oauth.complete(query(await oauth.authorize_url(6, G, 2))["state"], "c", G)
    assert exc.value.kind == "account_taken" and exc.value.user_id == 6 and exc.value.pending_id == 2
    assert await tokens.grant(6, G) is None and (await tokens.grant(5, G)).status == "ACTIVE"


@pytest.mark.parametrize("provider", [G, S], ids=["google", "slack"])
async def test_two_callbacks_racing_for_one_account_leave_exactly_one_owner(oauth, tokens, vendor, provider):
    import asyncio

    (google_vendor if provider is G else slack_vendor)(vendor)
    states = [query(await oauth.authorize_url(u, provider))["state"] for u in (5, 6, 7)]
    results = await asyncio.gather(*(oauth.complete(st, "c", provider) for st in states),
                                   return_exceptions=True)
    won = [r for r in results if not isinstance(r, BaseException)]
    lost = [r for r in results if isinstance(r, OAuthError) and r.kind == "account_taken"]
    assert len(won) == 1 and len(lost) == 2
    owners = [u for u in (5, 6, 7) if await tokens.grant(u, provider) is not None]
    assert owners == [won[0].user_id]


async def test_a_revoked_grant_does_not_lock_the_account_for_ever(oauth, tokens, vendor):
    google_vendor(vendor)
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    await tokens.mark(5, G, "REVOKED")
    await oauth.complete(query(await oauth.authorize_url(6, G))["state"], "c", G)
    assert await tokens.grant(5, G) is None and (await tokens.grant(6, G)).status == "ACTIVE"


async def test_a_user_can_switch_to_another_account_and_free_the_old_one(oauth, tokens, vendor):
    google_vendor(vendor, email="one@x.com")
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    google_vendor(vendor, email="two@x.com")
    await oauth.complete(query(await oauth.authorize_url(5, G))["state"], "c", G)
    google_vendor(vendor, email="one@x.com")
    await oauth.complete(query(await oauth.authorize_url(6, G))["state"], "c", G)  # one@x.com is free again
    assert (await tokens.account(6, G))["email"] == "one@x.com"


async def test_accounts_without_a_vendor_id_do_not_collide(tokens):
    for user in (1, 2):
        await tokens.save(user, G, account={"scopes": []}, access_token="a", refresh_token="r",
                          expires_at=None)
    assert len(await tokens.grants(1)) == len(await tokens.grants(2)) == 1


async def test_the_same_account_in_different_providers_is_fine(tokens):
    await tokens.save(1, G, account={"email": "a@x.com"}, access_token="a", refresh_token="r",
                      expires_at=None)
    await tokens.save(2, S, account={"user_id": "a@x.com"}, access_token="a", refresh_token="r",
                      expires_at=None)
