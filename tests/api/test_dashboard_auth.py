# ruff: noqa: E501
"""Dashboard API: sessions, CSRF, Telegram link sign-in, Google sign-in."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from mavis.access.codes import deep_link_param
from mavis.domain import timeutil
from mavis.domain.events import Event, EventType, Trust
from mavis.store import db as dbm
from mavis.store.models import User, UserEmail, WebSession
from mavis.store.repo import invites, users
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import GOOGLE_TOKEN_URL
from mavis.web import emails, google_signin, logins, sessions
from tests.api.dash_helpers import *  # noqa: F403 - fixtures and helpers
from tests.api.dash_helpers import BASE, id_token, new_client, query, signed_in

API = "/api/v1"


async def user_count() -> int:
    async with dbm.Session() as s:
        return int(await s.scalar(select(func.count()).select_from(User)) or 0)


# --- off switch, errors, sessions, CSRF -------------------------------------------------------------------


async def test_everything_is_404_while_the_dashboard_is_off(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("DASHBOARD_ENABLED", "false")
    get_settings.cache_clear()
    async with new_client(app) as c:
        for method, path in (("GET", "/me"), ("POST", "/auth/telegram/start"), ("GET", "/auth/google/start")):
            r = await c.request(method, f"{API}{path}")
            assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_unauthenticated_requests_get_a_plain_401(app):
    async with new_client(app) as c:
        r = await c.get(f"{API}/me")
        assert r.status_code == 401 and set(r.json()) == {"error", "message"}
        assert (await c.get(f"{API}/nope")).json()["error"] == "not_found"
        assert (await c.post(f"{API}/auth/telegram/start", json={"invite": 5})).json()["error"] == "invalid_request"


async def test_me_hands_out_the_csrf_token_and_mutations_need_it(app):
    c, csrf = await signed_in(app, 1)
    async with c:
        me = (await c.get(f"{API}/me")).json()
        assert me["user_id"] == "1" and me["csrf"] == csrf["X-Mavis-CSRF"]
        assert me["channels"]["telegram"]["open_url"] == "https://telegram.me/MavisTestBot"
        assert (await c.patch(f"{API}/preferences", json={"name": "Priya"})).status_code == 403
        assert (await c.patch(f"{API}/preferences", json={"name": "Priya"},
                              headers={"X-Mavis-CSRF": "wrong"})).status_code == 403
        r = await c.patch(f"{API}/preferences", json={"name": "Priya"}, headers=csrf)
        assert r.status_code == 200 and r.json()["name"] == "Priya"


async def test_csrf_token_of_another_session_is_refused(app):
    a, csrf_a = await signed_in(app, 1)
    b, _ = await signed_in(app, 1)
    async with a, b:
        assert (await b.patch(f"{API}/preferences", json={"name": "X"}, headers=csrf_a)).status_code == 403


async def test_session_expires_after_thirty_days(app, clock):
    c, _ = await signed_in(app, 1)
    async with c:
        assert (await c.get(f"{API}/me")).status_code == 200
        clock.advance(days=29)
        assert (await c.get(f"{API}/me")).status_code == 200
        clock.advance(days=2)
        assert (await c.get(f"{API}/me")).status_code == 401
    assert await sessions.count(1) == 0  # the expired row is gone


async def test_logout_ends_one_session_and_logout_everywhere_ends_all(app):
    a, csrf_a = await signed_in(app, 1)
    b, csrf_b = await signed_in(app, 1)
    other, _ = await signed_in(app, 2)
    async with a, b, other:
        assert (await a.post(f"{API}/auth/logout", json={"all": False}, headers=csrf_a)).status_code == 204
        assert (await a.get(f"{API}/me")).status_code == 401
        assert (await b.get(f"{API}/me")).status_code == 200
        assert (await b.post(f"{API}/auth/logout", json={"all": True}, headers=csrf_b)).status_code == 204
        assert (await b.get(f"{API}/me")).status_code == 401
        assert (await other.get(f"{API}/me")).status_code == 200
    assert await sessions.count(1) == 0


async def test_logout_needs_csrf(app):
    c, _ = await signed_in(app, 1)
    async with c:
        assert (await c.post(f"{API}/auth/logout", json={})).status_code == 403
        assert (await c.get(f"{API}/me")).status_code == 200


async def test_a_banned_user_loses_the_session(app):
    c, _ = await signed_in(app, 1)
    async with c:
        await users.update(1, status="banned")
        assert (await c.get(f"{API}/me")).status_code == 401
    assert await sessions.count(1) == 0


async def test_cookie_flags_and_only_the_hash_is_stored(app):
    token = await sessions.create(1)
    async with dbm.Session() as s:
        row = (await s.scalars(select(WebSession))).one()
    assert row.id != token and token not in (row.id, row.csrf_hash)
    c = new_client(app)
    async with c:
        r = await c.post(f"{API}/auth/telegram/start", json={})
        flags = r.headers["set-cookie"].lower()
        assert "httponly" in flags and "secure" in flags and "samesite=lax" in flags


async def test_auth_endpoints_are_rate_limited_per_ip(app):
    async with new_client(app) as c:
        codes = [(await c.post(f"{API}/auth/telegram/start", json={},
                               headers={"x-forwarded-for": "198.51.100.7"})).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and codes[30:] == [429, 429]


async def test_requests_are_rate_limited_per_session(app, monkeypatch):
    from mavis.api.dashboard import common

    monkeypatch.setattr(common, "SESSION_PER_MIN", 3)
    c, _ = await signed_in(app, 1)
    async with c:
        codes = [(await c.get(f"{API}/me")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


# --- Telegram link sign-in --------------------------------------------------------------------------------


async def ask(nonce, user_id, event_id="tg:ask"):
    """The person opens the deep link in Telegram and presses Start."""
    from mavis.web import login_gate

    ev = Event(id=event_id, user_id=user_id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
               source="telegram", payload={"text": f"/start login_{nonce}"}, trust=Trust.USER)
    assert await login_gate.web_login_gate(ev) is False


async def tap(user_id, data, event_id="tg:tap"):
    ev = Event(id=event_id, user_id=user_id, type=EventType.BUTTON_PRESSED, occurred_at=timeutil.now(),
               source="telegram", payload={"data": data}, trust=Trust.USER)
    await logins.on_button(ev, data)


async def approve_in_bot(nonce, user_id):
    """Start, then the Approve button, from the same chat."""
    await ask(nonce, user_id)
    await tap(user_id, f"wl:y:{nonce}")


async def outbox_rows(user_id):
    from mavis.store.models import OutboxMessage

    async with dbm.Session() as s:
        return list((await s.scalars(select(OutboxMessage).where(OutboxMessage.user_id == user_id))).all())


async def start_login(c, **body):
    r = await c.post(f"{API}/auth/telegram/start", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_telegram_sign_in_end_to_end(app):
    async with new_client(app) as c:
        started = await start_login(c)
        nonce = started["nonce"]
        assert started["deep_link"] == f"https://telegram.me/MavisTestBot?start=login_{nonce}"
        assert len(deep_payload := started["deep_link"].split("start=")[1]) <= 64 and deep_payload
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json() == {"status": "pending"}
        assert len(started["code"]) == 4 and started["code"].isdigit()
        await approve_in_bot(nonce, 3)  # user 3 pressed Start, then Approve
        r = await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})
        assert r.json() == {"status": "ok"} and sessions.COOKIE in r.headers["set-cookie"]
        assert (await c.get(f"{API}/me")).json()["user_id"] == "3"
        # single use
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json() == {"status": "expired"}


async def test_a_forwarded_link_cannot_sign_in_another_browser(app):
    async with new_client(app) as mine, new_client(app) as attacker:
        nonce = (await start_login(mine))["nonce"]
        await approve_in_bot(nonce, 3)
        r = await attacker.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})
        assert r.json() == {"status": "expired"} and (await attacker.get(f"{API}/me")).status_code == 401
        assert (await mine.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "ok"


async def test_nonce_expires_after_ten_minutes_and_binds_once(app, clock):
    async with new_client(app) as c:
        nonce = (await start_login(c))["nonce"]
        await approve_in_bot(nonce, 3)
        await ask(nonce, 4)
        assert await logins.approve(nonce, 4) is False  # another chat cannot take over a bound nonce
        clock.advance(minutes=11)
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json() == {"status": "expired"}
        fresh = (await start_login(c))["nonce"]
        await ask(fresh, 3)
        clock.advance(minutes=11)
        assert await logins.approve(fresh, 3) is False


async def test_garbage_nonce_is_expired_not_an_error(app):
    async with new_client(app) as c:
        await start_login(c)
        for bad in ("x", "0" * 32, "../../etc"):
            assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": bad})).json() == {"status": "expired"}


def test_start_payload_parsing():
    nonce = "a" * 32
    assert logins.parse_start(f"/start login_{nonce}").invite is None
    assert logins.parse_start(f"/start@MavisTestBot login_{nonce}").nonce == nonce
    login = logins.parse_start(f"/start login_{nonce}_MAV0123456789")
    assert login.invite == "0123456789"
    assert logins.parse_start("/start MAV0123456789") is None and logins.parse_start("hello") is None
    assert logins.parse_start(f"/start login_{nonce[:-1]}") is None
    assert len(f"login_{nonce}_{deep_link_param('0123456789')}") <= 64


async def test_invite_rides_in_the_deep_link_and_is_validated(app):
    async with new_client(app) as c:
        r = await start_login(c, invite="mav-01234-56789")
        assert r["deep_link"].endswith(f"_{deep_link_param('0123456789')}")
        bad = await c.post(f"{API}/auth/telegram/start", json={"invite": "nope"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_invite"


async def test_the_bot_signs_in_an_active_chat_and_says_so(app, channel):
    from mavis.channels.outbox_sender import OutboxSender
    from mavis.web import login_gate

    async with new_client(app) as c:
        started = await start_login(c)
        nonce, code = started["nonce"], started["code"]
        await users.update(3, telegram_chat_id=30003)
        ev = Event(id="tg:1", user_id=3, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                   source="telegram", payload={"text": f"/start login_{nonce}"}, trust=Trust.USER)
        assert await login_gate.web_login_gate(ev) is False
        await OutboxSender(channel).run_once()
        (text,) = channel.texts
        assert "Check that the code matches" in text and f" {logins.spaced(code)}" in text and "Approve" in text
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "pending"
        await tap(3, f"wl:y:{nonce}")
        await OutboxSender(channel).run_once()
        assert channel.texts[-1] == "You're signed in on the web. You can close this."
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "ok"


async def test_a_new_chat_redeems_the_invite_in_the_link_then_is_signed_in(app, invite_mode, channel):
    from mavis.access import gate

    stranger, _ = await users.get_or_create_by_chat(777001, "Lena")
    _, plain = await invites.mint(created_by=1)
    async with new_client(app) as c:
        nonce = (await start_login(c, invite=plain))["nonce"]
        payload = f"/start login_{nonce}_{deep_link_param(plain)}"
        ev = Event(id="tg:9", user_id=stranger.id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                   source="telegram", payload={"text": payload, "pending": True}, trust=Trust.USER)
        assert await gate.access_gate(ev) is False
        assert (await users.get(stranger.id)).status == "active"
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "pending"
        await tap(stranger.id, f"wl:y:{nonce}")
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "ok"
        assert (await c.get(f"{API}/me")).json()["user_id"] == str(stranger.id)


async def test_a_new_chat_without_an_invite_is_not_signed_in(app, invite_mode, channel):
    from mavis.access import gate

    stranger, _ = await users.get_or_create_by_chat(777002, "Lena")
    async with new_client(app) as c:
        nonce = (await start_login(c))["nonce"]
        ev = Event(id="tg:10", user_id=stranger.id, type=EventType.USER_MESSAGE, occurred_at=timeutil.now(),
                   source="telegram", payload={"text": f"/start login_{nonce}", "pending": True},
                   trust=Trust.USER)
        await gate.access_gate(ev)
        assert (await users.get(stranger.id)).status == "pending"
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "pending"
        await logins.request_approval(stranger.id, logins.StartLogin(nonce, None), "tg:11")
        assert await logins.approve(nonce, stranger.id) is False  # pending users are not admitted in invite mode


# --- Google sign-in ---------------------------------------------------------------------------------------


async def begin_google(c, confirm: bool = False):
    r = await c.get(f"{API}/auth/google/start", params={"confirm": "1"} if confirm else None)
    assert r.status_code == 302, r.text
    return query(r.headers["location"])


def token_route(vendor, **claims):
    import httpx

    def answer(request):
        return httpx.Response(200, json={"access_token": "ya29", "id_token": id_token(**claims)})

    vendor.routes[GOOGLE_TOKEN_URL] = answer


async def google_callback(c, vendor, q, **claims):
    token_route(vendor, nonce=q["nonce"], **claims)
    return await c.get(f"{API}/auth/google/callback", params={"code": "c", "state": q["state"]})


async def test_google_start_uses_openid_email_profile_pkce_and_its_own_redirect(app):
    async with new_client(app) as c:
        r = await c.get(f"{API}/auth/google/start")
        assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        q = query(r.headers["location"])
        assert q["scope"] == "openid email profile" and q["code_challenge_method"] == "S256"
        assert q["redirect_uri"] == f"{BASE}/api/v1/auth/google/callback"
        assert q["client_id"] == "gid" and q["state"] and q["nonce"]
        assert "mavis_gsi" in r.headers["set-cookie"]


async def test_google_signs_in_the_user_whose_email_is_confirmed(app, google):
    assert await emails.confirm(5, "Me@Kripya.com", "telegram_link")
    async with new_client(app) as c:
        q = await begin_google(c)
        r = await google_callback(c, google, q)
        assert r.status_code == 302 and r.headers["location"] == "/workspace"
        assert (await c.get(f"{API}/me")).json()["user_id"] == "5"
        assert (await c.get(f"{API}/me")).json()["email"] == "me@kripya.com"


async def test_a_native_google_grant_alone_never_signs_anyone_in(app, google, tokens):
    await tokens.save(6, NativeProvider.GOOGLE, account={"email": "me@kripya.com", "scopes": []},
                      access_token="a", refresh_token="r", expires_at=None)
    async with new_client(app) as c:
        r = await google_callback(c, google, await begin_google(c))
        assert r.headers["location"].startswith("/link-telegram?i=")
        assert (await c.get(f"{API}/me")).status_code == 401


async def test_unknown_email_never_creates_a_user_and_goes_to_link_telegram(app, google):
    before = await user_count()
    async with new_client(app) as c:
        r = await google_callback(c, google, await begin_google(c), email="stranger@x.com")
        assert r.headers["location"].startswith("/link-telegram?i=")
        assert (await c.get(f"{API}/me")).status_code == 401
    assert await user_count() == before


async def test_completing_telegram_after_google_confirms_that_email(app, google):
    async with new_client(app) as c:
        r = await google_callback(c, google, await begin_google(c), email="new@kripya.com")
        link_id = r.headers["location"].split("?i=")[1]
        nonce = (await start_login(c, link_id=link_id))["nonce"]
        await approve_in_bot(nonce, 7)
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "ok"
    assert await emails.user_for("new@kripya.com") == 7
    async with new_client(app) as c2:  # next time Google alone signs them in
        r = await google_callback(c2, google, await begin_google(c2), email="new@kripya.com")
        assert r.headers["location"] == "/workspace"


async def test_an_unknown_link_id_is_refused(app):
    async with new_client(app) as c:
        r = await c.post(f"{API}/auth/telegram/start", json={"link_id": "abc"})
        assert r.status_code == 400 and r.json()["error"] == "link_expired"


@pytest.mark.parametrize("claims", [
    {"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": 1}, {"email_verified": False},
    {"nonce": "not-mine"}, {"kid": "unknown-key"}, {"sub": ""}])
async def test_bad_id_tokens_are_refused(app, google, claims):
    await emails.confirm(5, "me@kripya.com", "telegram_link")
    async with new_client(app) as c:
        q = await begin_google(c)
        claims = {"nonce": q["nonce"], **claims}
        token_route(google, **claims)
        r = await c.get(f"{API}/auth/google/callback", params={"code": "c", "state": q["state"]})
        assert r.status_code == 302 and r.headers["location"].startswith("/login?error=google_")
        assert (await c.get(f"{API}/me")).status_code == 401


async def test_wrong_state_denied_and_forged_signature_are_refused(app, google):
    await emails.confirm(5, "me@kripya.com", "telegram_link")
    async with new_client(app) as c:
        q = await begin_google(c)
        r = await c.get(f"{API}/auth/google/callback", params={"code": "c", "state": "other"})
        assert r.headers["location"] == "/login?error=google_bad_state"
        r = await c.get(f"{API}/auth/google/callback", params={"error": "access_denied", "state": q["state"]})
        assert r.headers["location"] == "/login?error=google_denied"


async def test_a_token_signed_by_another_key_is_refused(app, google):
    import httpx
    from cryptography.hazmat.primitives.asymmetric import rsa

    await emails.confirm(5, "me@kripya.com", "telegram_link")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with new_client(app) as c:
        q = await begin_google(c)
        forged = id_token(q["nonce"], key=other)
        google.routes[GOOGLE_TOKEN_URL] = lambda r: httpx.Response(200, json={"id_token": forged})
        r = await c.get(f"{API}/auth/google/callback", params={"code": "c", "state": q["state"]})
        assert r.headers["location"] == "/login?error=google_bad_token"
        assert (await c.get(f"{API}/me")).status_code == 401


async def test_jwks_is_cached(app, google):
    await emails.confirm(5, "me@kripya.com", "telegram_link")
    for _ in range(2):
        async with new_client(app) as c:
            await google_callback(c, google, await begin_google(c))
    assert len(google.to(google_signin.JWKS_URL)) == 1


async def test_confirming_an_email_in_preferences_needs_the_same_session(app, google):
    c, _ = await signed_in(app, 8)
    async with c:
        q = await begin_google(c, confirm=True)
        r = await google_callback(c, google, q, email="mine@kripya.com")
        assert r.headers["location"] == "/preferences?email=confirmed"
    assert await emails.user_for("mine@kripya.com") == 8
    # an address that belongs to someone else cannot be taken
    c2, _ = await signed_in(app, 9)
    async with c2:
        r = await google_callback(c2, google, await begin_google(c2, confirm=True), email="mine@kripya.com")
        assert r.headers["location"] == "/preferences?email=taken"
    async with new_client(app) as anon:  # no session: confirm mode is not available
        assert (await anon.get(f"{API}/auth/google/start", params={"confirm": "1"})).headers["location"] == "/login"


async def test_google_signin_is_off_unless_enabled(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("GOOGLE_SIGNIN_ENABLED", "false")
    get_settings.cache_clear()
    async with new_client(app) as c:
        r = await c.get(f"{API}/auth/google/start")
        assert r.headers["location"] == "/login?error=google_unavailable"


async def test_confirmed_emails_are_unique(app):
    assert await emails.confirm(1, "a@x.com", "google_grant") and not await emails.confirm(2, "A@x.com", "google_signin")
    async with dbm.Session() as s:
        assert int(await s.scalar(select(func.count()).select_from(UserEmail))) == 1


# --- approval in the bot (a forwarded link must not sign anyone in) ---------------------------------------


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


async def test_pressing_start_alone_never_signs_the_browser_in(app, channel):
    """The attack: the nonce belongs to the attacker's browser, the victim presses Start on the forwarded link."""
    async with new_client(app) as attacker:
        nonce = (await start_login(attacker))["nonce"]
        await ask(nonce, 3)  # the victim pressed Start
        poll = await attacker.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})
        assert poll.json() == {"status": "pending"} and (await attacker.get(f"{API}/me")).status_code == 401
        await tap(3, f"wl:n:{nonce}")  # the victim sees a stranger's request and taps Not me
        assert (await attacker.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json() == {"status": "expired"}
        assert await logins.approve(nonce, 3) is False  # a late Approve on the same message is dead too


async def test_the_confirmation_shows_what_is_asking_and_the_code(app, channel):
    from mavis.channels.outbox_sender import OutboxSender

    async with new_client(app) as c:
        r = await c.post(f"{API}/auth/telegram/start", json={},
                         headers={"user-agent": UA, "x-forwarded-for": "203.0.113.77", "cf-ipcountry": "DE"})
        started = r.json()
        await users.update(3, telegram_chat_id=30003)
        await ask(started["nonce"], 3)
        await OutboxSender(channel).run_once()
        (text,) = channel.texts
        assert "Chrome on Linux" in text and "DE (near 203.0.x.x)" in text and "UTC" in text
        assert "203.0.113.77" not in text  # the address is masked
        digits = " ".join(started["code"])
        assert f"Check that the code matches the one on the sign in page: {digits}" in text
        assert "\u2014" not in text and "\u2013" not in text
        rows = await outbox_rows(3)
        assert [[b["label"] for b in line] for line in rows[0].buttons] == [["Approve", "Not me"]]
        assert [[b["data"] for b in line] for line in rows[0].buttons] == [[f"wl:y:{started['nonce']}", f"wl:n:{started['nonce']}"]]


def test_agent_and_address_descriptions():
    assert logins.describe_agent(UA) == "Chrome on Linux"
    assert logins.describe_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1") == "Safari on iOS"
    assert logins.describe_agent("") == "An unknown browser"
    assert logins.mask_ip("198.51.100.9") == "198.51.x.x" and logins.mask_ip("2001:db8:1:2::3") == "2001:db8:x:x"
    assert logins.describe_place("198.51.100.9", "") == "198.51.x.x"


async def test_approve_is_single_use_for_the_person_asked_and_expires_with_the_nonce(app, clock):
    async with new_client(app) as c:
        nonce = (await start_login(c))["nonce"]
        assert await logins.approve(nonce, 3) is False  # never asked: Start was not pressed
        await ask(nonce, 3)
        assert await logins.approve(nonce, 4) is False  # not the person it was sent to
        assert await logins.approve(nonce, 3) is True
        assert await logins.approve(nonce, 3) is False  # the button works once
        late = (await start_login(c))["nonce"]
        await ask(late, 3)
        clock.advance(minutes=11)
        assert await logins.approve(late, 3) is False


async def test_the_login_page_code_and_deep_link_carry_no_session(app):
    async with new_client(app) as c:
        started = await start_login(c)
        assert set(started) == {"nonce", "deep_link", "expires_at", "code", "link_email"}
        assert sessions.COOKIE not in c.cookies


async def test_signing_in_revokes_the_session_the_browser_already_held(app):
    c, _ = await signed_in(app, 1)
    old = c.cookies.get(sessions.COOKIE)
    async with c:
        nonce = (await start_login(c))["nonce"]
        await approve_in_bot(nonce, 3)
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json()["status"] == "ok"
        assert (await c.get(f"{API}/me")).json()["user_id"] == "3"
    assert await sessions.lookup(old) is None
    assert await sessions.count(1) == 0


async def test_the_session_cookie_is_a_host_cookie(app):
    assert sessions.COOKIE == "__Host-mavis_session"
    async with new_client(app) as c:
        nonce = (await start_login(c))["nonce"]
        await approve_in_bot(nonce, 3)
        flags = (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).headers["set-cookie"].lower()
        assert "__host-mavis_session=" in flags and "secure" in flags and "path=/;" in flags + ";"
        assert "domain" not in flags


# --- linking a Google address through Telegram -----------------------------------------------------------


async def stash(c, google, email="new@kripya.com"):
    r = await google_callback(c, google, await begin_google(c), email=email)
    return r.headers["location"].split("?i=")[1]


async def test_the_link_url_carries_no_address_and_only_its_browser_can_use_it(app, google):
    async with new_client(app) as c, new_client(app) as other:
        link_id = await stash(c, google)
        assert "kripya" not in link_id and "@" not in link_id
        assert (await c.get(f"{API}/auth/telegram/link", params={"i": link_id})).json() == {"email": "new@kripya.com"}
        # the same URL in another browser (forwarded) is useless
        assert (await other.get(f"{API}/auth/telegram/link", params={"i": link_id})).status_code == 404
        r = await other.post(f"{API}/auth/telegram/start", json={"link_id": link_id})
        assert r.status_code == 400 and r.json()["error"] == "link_expired"
        assert (await c.post(f"{API}/auth/telegram/start", json={"link_id": link_id})).status_code == 200


async def test_the_link_is_spent_once_approved_and_expires(app, google, clock):
    async with new_client(app) as c:
        link_id = await stash(c, google)
        nonce = (await start_login(c, link_id=link_id))["nonce"]
        await approve_in_bot(nonce, 7)
        assert (await c.post(f"{API}/auth/telegram/start", json={"link_id": link_id})).status_code == 400
    async with new_client(app) as c2:
        link2 = await stash(c2, google, email="later@kripya.com")
        clock.advance(minutes=16)
        assert (await c2.post(f"{API}/auth/telegram/start", json={"link_id": link2})).status_code == 400


async def test_an_address_is_linked_only_after_the_person_approves(app, google, channel):
    from mavis.channels.outbox_sender import OutboxSender

    async with new_client(app) as c:
        link_id = await stash(c, google, email="attacker@evil.example")
        nonce = (await start_login(c, link_id=link_id))["nonce"]
        await users.update(3, telegram_chat_id=30003)
        await ask(nonce, 3)  # the victim opens the forwarded link
        await OutboxSender(channel).run_once()
        assert "links the Google address attacker@evil.example to your Mavis account" in channel.texts[0]
        assert await emails.user_for("attacker@evil.example") is None
        await tap(3, f"wl:n:{nonce}")
        assert await emails.user_for("attacker@evil.example") is None
        assert (await c.get(f"{API}/auth/telegram/poll", params={"nonce": nonce})).json() == {"status": "expired"}


# --- public config ----------------------------------------------------------------------------------------


async def test_public_config_needs_no_session_and_reflects_settings(app):
    async with new_client(app) as c:
        r = await c.get(f"{API}/config")
        assert r.status_code == 200
        assert r.json() == {"bot_username": "MavisTestBot", "bot_url": "https://telegram.me/MavisTestBot",
                            "slack_enabled": True, "google_signin_enabled": True}


async def test_public_config_without_a_bot_username_has_no_link(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "")
    get_settings.cache_clear()
    async with new_client(app) as c:
        body = (await c.get(f"{API}/config")).json()
        assert body["bot_username"] is None and body["bot_url"] is None


async def test_public_config_is_404_while_the_dashboard_is_off(app, monkeypatch):
    from mavis.config import get_settings

    monkeypatch.setenv("DASHBOARD_ENABLED", "false")
    get_settings.cache_clear()
    async with new_client(app) as c:
        assert (await c.get(f"{API}/config")).status_code == 404
