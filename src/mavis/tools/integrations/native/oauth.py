"""Native OAuth: authorize URLs, signed single-use state with a sealed PKCE verifier, code exchange, revoke.

State travels through the browser, so the PKCE verifier inside it is encrypted (AES-GCM bound to user,
provider, expiry and nonce) and the whole body is MAC'd. The nonce is what makes it single use: it is
stored when the link is issued and deleted when the callback completes, so a replayed state fails.

Nothing here logs or raises with a token, a client secret or a vendor response body: failures become
OAuthError with a stable `kind`, raised `from None` so the request is not chained.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete

from mavis.access import admission
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.store import db as dbm
from mavis.store.models import NativeOAuthState
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.http import TOKEN_BYTES, ResponseTooLarge, send_capped
from mavis.tools.integrations.native.tokens import (
    GOOGLE_TOKEN_URL,
    SLACK_TOKEN_URL,
    AccountTaken,
    NativeTokenStore,
    UserNotAdmitted,
)

STATE_TTL_S = 600

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_REVOKE_URL = "https://slack.com/api/auth.revoke"
SLACK_OPEN_DM_URL = "https://slack.com/api/conversations.open"

_AUTH = "https://www.googleapis.com/auth/"
# Full Workspace read and write. A broader scope covers the narrower ones (gmail.modify reads, calendar
# covers events, drive covers every Drive read, contacts covers contacts.readonly), so none of the
# narrower variants is asked for. router.ACTION_SCOPES still honours older grants made with them.
GOOGLE_SCOPES = ("openid", "email", "profile") + tuple(
    _AUTH + name for name in (
        "gmail.modify", "gmail.send", "gmail.compose",
        "calendar", "drive", "documents", "spreadsheets", "presentations",
        "forms.body.readonly", "forms.responses.readonly",
        "tasks", "contacts", "contacts.other.readonly", "directory.readonly",
        "meetings.space.created", "meetings.space.readonly",
    )
)
SLACK_USER_SCOPES = (
    "channels:history", "groups:history", "im:history", "mpim:history",
    "channels:read", "groups:read", "im:read", "mpim:read",
    "users:read", "users:read.email", "chat:write", "search:read",
)

# Bot scopes: Mavis as a bot user in the workspace (DM and @mention chat, cards, reactions, files).
SLACK_BOT_SCOPES = (
    "chat:write", "im:history", "im:read", "im:write", "app_mentions:read", "reactions:write",
    "users:read", "files:write", "commands",
)

_ERRORS = {"bad_state", "expired_state", "replayed_state", "denied", "exchange_failed", "not_configured",
           "wrong_provider", "account_taken", "not_allowed", "session_mismatch"}


class OAuthError(Exception):
    """kind is one of a fixed set; the message never carries vendor text."""

    def __init__(self, kind: str, *, user_id: int | None = None, pending_id: int | None = None,
                 origin: str | None = None) -> None:
        assert kind in _ERRORS
        super().__init__(kind)
        self.kind, self.user_id, self.pending_id, self.origin = kind, user_id, pending_id, origin


@dataclass(frozen=True)
class StatePayload:
    user_id: int
    provider: str
    verifier: str
    exp: int
    nonce: str


@dataclass(frozen=True)
class Completed:
    user_id: int
    provider: NativeProvider
    pending_id: int | None
    account: dict
    origin: str | None = None  # "web" when the consent was started from the dashboard


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> bytes:
    material = get_settings().native_token_kek
    if not material:
        raise OAuthError("not_configured")
    return material.encode()


def _mac_key() -> bytes:
    return hashlib.sha256(b"mavis-native-state-mac:" + _secret()).digest()


def _seal_key() -> bytes:
    return hashlib.sha256(b"mavis-native-state-seal:" + _secret()).digest()


def _aad(user_id: int, provider: str, exp: int, nonce: str) -> bytes:
    return f"{user_id}|{provider}|{exp}|{nonce}".encode()


def sign_state(user_id: int, provider: str, verifier: str, *, nonce: str, ttl_s: int = STATE_TTL_S) -> str:
    exp = int(time.time()) + ttl_s
    iv = os.urandom(12)
    sealed = AESGCM(_seal_key()).encrypt(iv, verifier.encode(), _aad(user_id, provider, exp, nonce))
    body = _b64(json.dumps({"u": user_id, "p": provider, "e": exp, "j": nonce, "n": _b64(iv),
                            "v": _b64(sealed)}, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_mac_key(), body.encode(), hashlib.sha256).digest())}"


def verify_state(state: str) -> StatePayload:
    parts = state.split(".")
    if len(parts) != 2 or not all(parts):
        raise OAuthError("bad_state")
    body, sig = parts
    good = _b64(hmac.new(_mac_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(good.encode(), sig.encode()):
        raise OAuthError("bad_state")
    try:
        raw = json.loads(_unb64(body))
        user_id, provider, exp, nonce = int(raw["u"]), str(raw["p"]), int(raw["e"]), str(raw["j"])
        verifier = AESGCM(_seal_key()).decrypt(_unb64(raw["n"]), _unb64(raw["v"]),
                                               _aad(user_id, provider, exp, nonce)).decode()
    except (ValueError, KeyError, TypeError, binascii.Error, InvalidTag):
        raise OAuthError("bad_state") from None
    if exp < time.time():
        raise OAuthError("expired_state", user_id=user_id)
    return StatePayload(user_id, provider, verifier, exp, nonce)


def redirect_uri(provider: NativeProvider) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/oauth/{provider.value}/callback"


# --- the confirmation step in front of a chat-issued consent link ---------------------------------------
# A link sent in Telegram is not bound to a browser, so whoever opens it would consent for the account that
# asked (a forwarded link attached the opener's Google to the sender's Mavis). The chat gets a link to our
# own page instead; it names the Mavis account being linked, and only "yes, that's me" continues to the
# provider. Single use, same lifetime as the consent state.
HANDOFF_PREFIX = "mavis:oauth:go:"


def handoff_url(provider: NativeProvider, token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/oauth/{provider.value}/go?t={token}"


async def handoff_link(url: str, user_id: int, provider: NativeProvider) -> str:
    """Park the provider's consent URL behind the confirmation page; returns the page's link."""
    from mavis import bus

    client = bus.get_redis()
    if client is None:  # tests and local dev without Redis: the consent URL itself
        return url
    token = secrets.token_urlsafe(24)
    await client.set(HANDOFF_PREFIX + token, json.dumps({"url": url, "user_id": user_id,
                                                         "provider": provider.value}), ex=STATE_TTL_S)
    return handoff_url(provider, token)


async def handoff(token: str, provider: NativeProvider, *, consume: bool) -> dict | None:
    """The parked consent for `token` ({url, user_id}), or None when unknown, expired or used."""
    from mavis import bus

    client = bus.get_redis()
    if client is None or not token or len(token) > 64:
        return None
    key = HANDOFF_PREFIX + token
    raw = await (client.getdel(key) if consume else client.get(key))
    if not raw:
        return None
    data = json.loads(raw)
    return data if data.get("provider") == provider.value else None


def configured(provider: NativeProvider) -> bool:
    s = get_settings()
    if not s.native_token_kek:
        return False
    if provider is NativeProvider.GOOGLE:
        return bool(s.google_oauth_client_id and s.google_oauth_client_secret)
    return bool(s.slack_client_id and s.slack_client_secret)


class NativeOAuth:
    def __init__(self, tokens: NativeTokenStore, client: httpx.AsyncClient) -> None:
        self.tokens, self._http = tokens, client

    # --- starting ------------------------------------------------------------------------------------

    async def authorize_url(self, user_id: int, provider: NativeProvider,
                            pending_id: int | None = None, *, origin: str | None = None,
                            session_hash: str | None = None) -> str:
        """`session_hash` binds a dashboard-started consent to the session that began it: the callback must
        present the same session, so a forwarded link cannot attach someone else's account."""
        if not configured(provider):
            raise OAuthError("not_configured")
        if not await admission.admitted_id(user_id):
            raise OAuthError("not_allowed")  # a banned, pending or deleted user gets no consent link
        nonce = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        now = timeutil.now()
        async with dbm.Session() as s:
            await s.execute(delete(NativeOAuthState).where(NativeOAuthState.expires_at < now))
            s.add(NativeOAuthState(nonce=nonce, user_id=user_id, provider=provider.value,
                                   pending_id=pending_id, origin=origin, session_hash=session_hash,
                                   expires_at=now + timedelta(seconds=STATE_TTL_S)))
            await s.commit()
        state = sign_state(user_id, provider.value, verifier, nonce=nonce)
        st = get_settings()
        if provider is NativeProvider.GOOGLE:
            q = {"response_type": "code", "client_id": st.google_oauth_client_id,
                 "redirect_uri": redirect_uri(provider), "scope": " ".join(GOOGLE_SCOPES),
                 "state": state, "access_type": "offline", "prompt": "consent",
                 "include_granted_scopes": "true",
                 "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
                 "code_challenge_method": "S256"}
            return f"{GOOGLE_AUTH_URL}?{urlencode(q)}"
        q = {"client_id": st.slack_client_id, "redirect_uri": redirect_uri(provider),
             "scope": ",".join(SLACK_BOT_SCOPES), "user_scope": ",".join(SLACK_USER_SCOPES),
             "state": state}
        return f"{SLACK_AUTH_URL}?{urlencode(q)}"

    # --- finishing -----------------------------------------------------------------------------------

    async def _consume(self, st: StatePayload, provider: NativeProvider
                       ) -> tuple[int | None, str | None, str | None]:
        """Delete the nonce row (single use); returns the pending id, the origin and the bound session."""
        async with dbm.Session() as s:
            row = await s.get(NativeOAuthState, st.nonce)
            if (row is None or row.user_id != st.user_id or row.provider != st.provider
                    or timeutil.ensure_utc(row.expires_at) <= timeutil.now()):
                raise OAuthError("replayed_state", user_id=st.user_id)
            pending, origin, bound = row.pending_id, row.origin, row.session_hash
            res = await s.execute(delete(NativeOAuthState).where(NativeOAuthState.nonce == st.nonce))
            await s.commit()
        if not res.rowcount:  # lost a race with a concurrent callback
            raise OAuthError("replayed_state", user_id=st.user_id)
        return pending, origin, bound

    async def _state(self, state: str, provider: NativeProvider, session_hash: str | None = None
                     ) -> tuple[StatePayload, int | None, str | None]:
        st = verify_state(state)
        if st.provider != provider.value:
            raise OAuthError("wrong_provider")
        # the state is spent whoever it belonged to
        pending, origin, bound = await self._consume(st, provider)
        if bound is not None and not (session_hash and hmac.compare_digest(bound, session_hash)):
            raise OAuthError("session_mismatch", user_id=st.user_id, origin=origin)
        if not await admission.admitted_id(st.user_id):
            raise OAuthError("not_allowed")  # silent: a banned or deleted user is not messaged
        return st, pending, origin

    async def deny(self, state: str, provider: NativeProvider) -> tuple[int, int | None]:
        """The user (or the vendor) refused: spend the state and say whose it was."""
        user_id, pending, _ = await self.deny_with_origin(state, provider)
        return user_id, pending

    async def deny_with_origin(self, state: str, provider: NativeProvider, session_hash: str | None = None
                               ) -> tuple[int, int | None, str | None]:
        st, pending, origin = await self._state(state, provider, session_hash)
        return st.user_id, pending, origin

    async def complete(self, state: str, code: str, provider: NativeProvider,
                       session_hash: str | None = None) -> Completed:
        st, pending, origin = await self._state(state, provider, session_hash)
        try:
            return await self._complete(st, pending, code, provider, origin)
        except OAuthError as exc:
            exc.origin = origin
            raise

    async def _complete(self, st: StatePayload, pending: int | None, code: str, provider: NativeProvider,
                        origin: str | None) -> Completed:
        bot = None
        try:
            if provider is NativeProvider.GOOGLE:
                account, access, refresh, expires = await self._google(code, st.verifier)
            else:
                account, access, refresh, expires = await self._slack(code)
                bot = account.pop("_bot", None)  # the workspace bot token travels beside the user's
        except OAuthError as exc:
            raise OAuthError(exc.kind, user_id=st.user_id, pending_id=pending) from None
        key, value = (("email", account.get("email")) if provider is NativeProvider.GOOGLE
                      else ("user_id", account.get("user_id")))
        team = None if provider is NativeProvider.GOOGLE else str(account.get("team_id") or "")
        owner = await self.tokens.owner_of(provider, key, value, team) if value else None
        if owner is not None and owner != st.user_id:
            raise OAuthError("account_taken", user_id=st.user_id, pending_id=pending)
        try:
            await self.tokens.save(st.user_id, provider, account=account, access_token=access,
                                   refresh_token=refresh, expires_at=expires)
        except AccountTaken:  # the check above can lose a race; the database constraint cannot
            raise OAuthError("account_taken", user_id=st.user_id, pending_id=pending) from None
        except UserNotAdmitted:  # banned or deleted while the consent screen was open
            raise OAuthError("not_allowed") from None
        if bot is not None:
            try:
                await self._save_bot(st.user_id, bot)
            except UserNotAdmitted:
                await self.tokens.delete(st.user_id, provider)
                raise OAuthError("not_allowed") from None
        return Completed(st.user_id, provider, pending, account, origin)

    async def _post(self, url: str, **kw) -> dict:
        try:
            r = await send_capped(self._http, kw.pop("method", "POST"), url, max_bytes=TOKEN_BYTES, **kw)
        except (ResponseTooLarge, httpx.HTTPError):
            raise OAuthError("exchange_failed") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if not 200 <= r.status_code < 300 or not isinstance(body, dict):
            raise OAuthError("exchange_failed")
        return body

    async def _google(self, code: str, verifier: str):
        st = get_settings()
        body = await self._post(GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(NativeProvider.GOOGLE),
            "client_id": st.google_oauth_client_id, "client_secret": st.google_oauth_client_secret,
            "code_verifier": verifier})
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise OAuthError("exchange_failed")
        info = await self._post(GOOGLE_USERINFO_URL, method="GET",
                                headers={"Authorization": f"Bearer {access}"})
        email = info.get("email")
        if not isinstance(email, str) or not email:
            raise OAuthError("exchange_failed")
        scope = body.get("scope")
        account = {"email": email.lower(), "sub": str(info.get("sub") or ""),
                   "scopes": sorted(scope.split()) if isinstance(scope, str) else []}
        refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
        return account, access, refresh, _expiry(body)

    async def _slack(self, code: str):
        st = get_settings()
        body = await self._post(SLACK_TOKEN_URL, data={
            "code": code, "redirect_uri": redirect_uri(NativeProvider.SLACK),
            "client_id": st.slack_client_id, "client_secret": st.slack_client_secret})
        authed = body.get("authed_user")
        team = body.get("team")
        if body.get("ok") is not True or not isinstance(authed, dict):
            raise OAuthError("exchange_failed")
        access = authed.get("access_token")
        if not isinstance(access, str) or not access or not authed.get("id"):
            raise OAuthError("exchange_failed")
        scope = authed.get("scope")
        team = team if isinstance(team, dict) else {}
        account = {"team_id": str(team.get("id") or ""), "team_name": str(team.get("name") or ""),
                   "user_id": str(authed["id"]),
                   "scopes": sorted(scope.replace(",", " ").split()) if isinstance(scope, str) else []}
        refresh = authed.get("refresh_token") if isinstance(authed.get("refresh_token"), str) else None
        bot_token = body.get("access_token")
        if isinstance(bot_token, str) and bot_token and body.get("bot_user_id"):
            bot_scope = body.get("scope")
            account["_bot"] = {
                "token": bot_token,
                "account": {"team_id": account["team_id"], "team_name": account["team_name"],
                            "user_id": account["user_id"], "bot_user_id": str(body["bot_user_id"]),
                            "scopes": sorted(bot_scope.replace(",", " ").split())
                            if isinstance(bot_scope, str) else []},
            }
        return account, access, refresh, _expiry(authed)

    async def _save_bot(self, user_id: int, bot: dict) -> None:
        """Keep the bot token sealed beside the user grant, keyed by team and the authorizing user, and note
        the user's DM with the bot (so events in it are never read as third-party records). Best effort: a
        failure leaves Slack reading working without the bot."""
        account = dict(bot["account"])
        try:
            r = await send_capped(self._http, "POST", SLACK_OPEN_DM_URL, max_bytes=TOKEN_BYTES,
                                  headers={"Authorization": f"Bearer {bot['token']}"},
                                  json={"users": account["user_id"]})
            data = r.json()
            channel = (data.get("channel") or {}).get("id") if isinstance(data, dict) else None
            if isinstance(channel, str) and channel:
                account["dm"] = channel
        except (ResponseTooLarge, httpx.HTTPError, ValueError):
            pass
        try:
            await self.tokens.save(user_id, NativeProvider.SLACK_BOT, account=account,
                                   access_token=bot["token"], refresh_token=None, expires_at=None)
        except AccountTaken:
            pass

    # --- revoke --------------------------------------------------------------------------------------

    async def revoke(self, user_id: int, provider: NativeProvider) -> None:
        """Tell the vendor to drop the grant, then forget it locally. A vendor failure never blocks the
        local delete (the token would expire or stay revoked on its own)."""
        secrets_ = await self.tokens.reveal(user_id, provider)
        if secrets_ is not None:
            access, refresh = secrets_
            try:
                if provider is NativeProvider.GOOGLE:
                    await send_capped(self._http, "POST", GOOGLE_REVOKE_URL, max_bytes=TOKEN_BYTES,
                                      data={"token": refresh or access})
                else:
                    await send_capped(self._http, "POST", SLACK_REVOKE_URL, max_bytes=TOKEN_BYTES,
                                      headers={"Authorization": f"Bearer {access}"})
            except (ResponseTooLarge, httpx.HTTPError):
                pass
        await self.tokens.delete(user_id, provider)
        if provider is NativeProvider.SLACK:
            await self.tokens.delete(user_id, NativeProvider.SLACK_BOT)


def _expiry(body: dict):
    try:
        ttl = int(body["expires_in"]) if body.get("expires_in") else None
    except (TypeError, ValueError):
        ttl = None
    return timeutil.now() + timedelta(seconds=ttl) if ttl else None
