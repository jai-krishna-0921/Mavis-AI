"""NativeTokenStore: sealed per-user grants with single-flight refresh (implements TokenSource).

Tokens only ever live sealed in `native_grants`. Refresh is single-flight per (user, provider): an
asyncio lock inside a process, and a row lock (SELECT ... FOR UPDATE on Postgres) across processes, so two
workers never spend the same refresh token twice (Slack rotates it on use). A caller that asked for
`force=True` after a 401 while another caller was already refreshing reuses that fresh token instead of
refreshing again.

Vendor bodies never reach an exception message: failures become ReauthRequired (the grant is gone) or
IntegrationError with a status only.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.errors import IntegrationError
from mavis.store import db as dbm
from mavis.store.models import NativeGrant
from mavis.tools.integrations.native import crypto
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.http import TOKEN_BYTES, ResponseTooLarge, send_capped

ACTIVE, REVOKED, FAILED = "ACTIVE", "REVOKED", "FAILED"
REFRESH_AHEAD = timedelta(minutes=5)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# Vendor error codes that mean the grant itself is gone (the user must reconnect).
_GOOGLE_GONE = frozenset({"invalid_grant"})
_SLACK_GONE = frozenset({"invalid_refresh_token", "token_revoked", "account_inactive", "invalid_auth",
                         "token_expired", "not_authed", "invalid_grant"})


@dataclass(frozen=True)
class Grant:
    user_id: int
    provider: NativeProvider
    status: str
    account: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(self.account.get("scopes") or ())


def _grant(row: NativeGrant) -> Grant:
    return Grant(user_id=row.user_id, provider=NativeProvider(row.provider), status=row.status,
                 account=dict(row.account or {}), expires_at=timeutil.ensure_utc(row.expires_at),
                 updated_at=timeutil.ensure_utc(row.updated_at))


def _ctx(user_id: int, provider: NativeProvider, column: str) -> crypto.SealContext:
    return crypto.token_context(user_id, provider.value, column)


def _expiry(resp: dict, now: datetime) -> datetime | None:
    try:
        ttl = int(resp["expires_in"]) if resp.get("expires_in") else None
    except (TypeError, ValueError):
        ttl = None
    return now + timedelta(seconds=ttl) if ttl else None


class AccountTaken(Exception):
    """Another Mavis user already holds this vendor account (the database's unique constraint said so)."""


def account_key(provider: NativeProvider, account: dict[str, Any]) -> str | None:
    """The vendor's id for the account behind a grant: what must be unique per provider."""
    if provider is NativeProvider.GOOGLE:
        key = str(account.get("email") or "").lower()
    else:
        user, team = str(account.get("user_id") or ""), str(account.get("team_id") or "")
        key = f"{team}/{user}" if user and team else user
    return key[:255] or None


class _Gone(Exception):
    """Internal: the vendor says this refresh token is dead."""


class NativeTokenStore:
    def __init__(self, client: httpx.AsyncClient, *, clock: Callable[[], datetime] | None = None) -> None:
        self._http = client
        self._clock = clock or timeutil.now
        self._locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
            weakref.WeakValueDictionary())
        self._generation: dict[tuple[int, str], int] = {}

    def _lock(self, key: tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # --- reading -------------------------------------------------------------------------------------

    async def grant(self, user_id: int, provider: NativeProvider) -> Grant | None:
        async with dbm.Session() as s:
            row = await s.scalar(select(NativeGrant).where(
                NativeGrant.user_id == user_id, NativeGrant.provider == provider.value))
            return _grant(row) if row else None

    async def grants(self, user_id: int) -> list[Grant]:
        async with dbm.Session() as s:
            rows = await s.scalars(select(NativeGrant).where(NativeGrant.user_id == user_id))
            return [_grant(r) for r in rows]

    async def account(self, user_id: int, provider: NativeProvider) -> dict | None:
        g = await self.grant(user_id, provider)
        return g.account if g is not None and g.status == ACTIVE else None

    async def owner_of(self, provider: NativeProvider, account_key: str, value: str) -> int | None:
        """The user whose ACTIVE grant already holds this vendor account (one vendor account, one user)."""
        async with dbm.Session() as s:
            rows = await s.scalars(select(NativeGrant).where(
                NativeGrant.provider == provider.value, NativeGrant.status == ACTIVE))
            return next((r.user_id for r in rows if (r.account or {}).get(account_key) == value), None)

    async def reveal(self, user_id: int, provider: NativeProvider) -> tuple[str, str | None] | None:
        """(access, refresh) for a revoke call. Only the OAuth module uses this."""
        async with dbm.Session() as s:
            row = await s.scalar(select(NativeGrant).where(
                NativeGrant.user_id == user_id, NativeGrant.provider == provider.value))
            if row is None:
                return None
            return (crypto.open_text(row.access_token, _ctx(user_id, provider, "access_token")) or "",
                    crypto.open_text(row.refresh_token, _ctx(user_id, provider, "refresh_token")))

    # --- writing -------------------------------------------------------------------------------------

    async def save(self, user_id: int, provider: NativeProvider, *, account: dict[str, Any],
                   access_token: str, refresh_token: str | None, expires_at: datetime | None) -> None:
        """Create or replace the grant (a reconnect revives a REVOKED row). One vendor account belongs to
        one Mavis user: the unique (provider, account_key) constraint decides a race, and the loser gets
        AccountTaken. A REVOKED grant of another user does not hold the account."""
        now = self._clock()
        key = account_key(provider, account)
        sealed_access = crypto.seal_text(access_token, _ctx(user_id, provider, "access_token"))
        sealed_refresh = crypto.seal_text(refresh_token, _ctx(user_id, provider, "refresh_token"))
        try:
            async with dbm.Session() as s:
                if key is not None:
                    await s.execute(delete(NativeGrant).where(
                        NativeGrant.provider == provider.value, NativeGrant.account_key == key,
                        NativeGrant.user_id != user_id, NativeGrant.status != ACTIVE))
                row = await s.scalar(select(NativeGrant).where(
                    NativeGrant.user_id == user_id, NativeGrant.provider == provider.value))
                if row is None:
                    s.add(NativeGrant(user_id=user_id, provider=provider.value, account=account,
                                      account_key=key, access_token=sealed_access,
                                      refresh_token=sealed_refresh, expires_at=expires_at, status=ACTIVE,
                                      created_at=now, updated_at=now))
                else:
                    row.account, row.account_key = account, key
                    row.access_token, row.refresh_token = sealed_access, sealed_refresh
                    row.expires_at, row.status, row.updated_at = expires_at, ACTIVE, now
                await s.commit()
        except IntegrityError:
            holder = await self._holder(provider, key) if key is not None else None
            if holder is not None and holder != user_id:
                raise AccountTaken from None
            raise  # some other conflict: not ours to hide
        self._generation[(user_id, provider.value)] = self._generation.get((user_id, provider.value), 0) + 1

    async def _holder(self, provider: NativeProvider, key: str) -> int | None:
        async with dbm.Session() as s:
            return await s.scalar(select(NativeGrant.user_id).where(
                NativeGrant.provider == provider.value, NativeGrant.account_key == key))

    async def mark(self, user_id: int, provider: NativeProvider, status: str) -> None:
        async with dbm.Session() as s:
            row = await s.scalar(select(NativeGrant).where(
                NativeGrant.user_id == user_id, NativeGrant.provider == provider.value))
            if row is not None:
                row.status, row.updated_at = status, self._clock()
                await s.commit()

    async def delete(self, user_id: int, provider: NativeProvider) -> None:
        async with dbm.Session() as s:
            await s.execute(delete(NativeGrant).where(
                NativeGrant.user_id == user_id, NativeGrant.provider == provider.value))
            await s.commit()

    # --- TokenSource ---------------------------------------------------------------------------------

    def _fresh(self, expires_at: datetime | None) -> bool:
        return expires_at is None or timeutil.ensure_utc(expires_at) - REFRESH_AHEAD > self._clock()

    async def access_token(self, user_id: int, provider: NativeProvider, *, force: bool = False) -> str:
        key = (user_id, provider.value)
        seen = self._generation.get(key, 0)
        async with self._lock(key):
            if force and self._generation.get(key, 0) != seen:
                force = False  # a refresh finished while this caller waited: its token is the fresh one
            async with dbm.Session() as s:
                row = await s.scalar(select(NativeGrant).where(
                    NativeGrant.user_id == user_id, NativeGrant.provider == provider.value
                ).with_for_update())
                if row is None or row.status != ACTIVE:
                    raise ReauthRequired(f"{provider.value} is not connected")
                access = crypto.open_text(row.access_token, _ctx(user_id, provider, "access_token")) or ""
                if not force and self._fresh(row.expires_at):
                    return access
                refresh = crypto.open_text(row.refresh_token, _ctx(user_id, provider, "refresh_token"))
                if not refresh:
                    # expired (or rejected after a 401) and nothing to refresh with
                    row.status, row.updated_at = REVOKED, self._clock()
                    await s.commit()
                    raise ReauthRequired(f"{provider.value} access ended and cannot be refreshed")
                try:
                    resp = await self._refresh(provider, refresh)
                except _Gone:
                    row.status, row.updated_at = REVOKED, self._clock()
                    await s.commit()
                    raise ReauthRequired(f"{provider.value} access was revoked") from None
                now = self._clock()
                new_access = resp["access_token"]
                row.access_token = crypto.seal_text(new_access, _ctx(user_id, provider, "access_token"))
                if resp.get("refresh_token"):  # Slack rotates; Google keeps the old one
                    row.refresh_token = crypto.seal_text(
                        resp["refresh_token"], _ctx(user_id, provider, "refresh_token"))
                row.expires_at, row.updated_at = _expiry(resp, now), now
                await s.commit()
                self._generation[key] = self._generation.get(key, 0) + 1
                return new_access

    # --- vendor refresh ------------------------------------------------------------------------------

    async def _refresh(self, provider: NativeProvider, refresh_token: str) -> dict:
        st = get_settings()
        if provider is NativeProvider.GOOGLE:
            url, gone = GOOGLE_TOKEN_URL, _GOOGLE_GONE
            data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": st.google_oauth_client_id, "client_secret": st.google_oauth_client_secret}
        else:
            url, gone = SLACK_TOKEN_URL, _SLACK_GONE
            data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": st.slack_client_id, "client_secret": st.slack_client_secret}
        try:
            r = await send_capped(self._http, "POST", url, data=data, max_bytes=TOKEN_BYTES)
        except ResponseTooLarge:
            raise IntegrationError(f"{provider.value} token endpoint: response too large") from None
        except httpx.HTTPError:
            raise IntegrationError(f"{provider.value} token endpoint: could not reach the service") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        error = body.get("error")
        error = error if isinstance(error, str) else ""
        if provider is NativeProvider.SLACK and r.status_code == 200 and body.get("ok") is False:
            if error in gone:
                raise _Gone
            raise IntegrationError(f"{provider.value} token endpoint refused the refresh")
        if 200 <= r.status_code < 300 and isinstance(body.get("access_token"), str) and body["access_token"]:
            return body
        if error in gone:
            raise _Gone
        raise IntegrationError(f"{provider.value} token endpoint answered {r.status_code}",
                               status=r.status_code)
