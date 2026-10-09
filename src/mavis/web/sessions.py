"""Server-side dashboard sessions. The cookie holds a random token; only its sha256 is stored. The CSRF token
is derived from the cookie token (so /me can hand it out without storing it); its hash is stored too.
A session is valid only while its user is admitted (not banned, deleting or deleted)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update

from mavis.access import admission
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.store import db as dbm
from mavis.store.models import User, WebSession

COOKIE = "__Host-mavis_session"  # __Host-: Secure, Path=/, no Domain, so no sibling host can plant or read it
TOUCH_EVERY = timedelta(minutes=5)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hashlib.sha256(b"mavis-csrf:" + token.encode()).hexdigest()


def csrf_ok(token: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(csrf_for(token).encode(), (supplied or "").encode())


@dataclass(frozen=True)
class Active:
    token: str
    session_id: str
    user: User

    @property
    def csrf(self) -> str:
        return csrf_for(self.token)


async def create(user_id: int, *, ip: str = "", user_agent: str = "") -> str:
    """A new session for an admitted user; returns the cookie value."""
    token = secrets.token_urlsafe(32)
    now = timeutil.now()
    async with dbm.Session() as s:
        await s.execute(delete(WebSession).where(WebSession.expires_at < now))
        days = get_settings().dashboard_session_days
        s.add(WebSession(id=digest(token), user_id=user_id, csrf_hash=digest(csrf_for(token)), created_at=now,
                         last_seen_at=now, expires_at=now + timedelta(days=days), ip=ip[:64],
                         user_agent=user_agent[:200]))
        await s.commit()
    return token


async def lookup(token: str | None) -> Active | None:
    """The live session for this cookie value, or None (unknown, expired, or its user no longer admitted)."""
    if not token or len(token) > 200:
        return None
    sid = digest(token)
    now = timeutil.now()
    async with dbm.Session() as s:
        row = await s.get(WebSession, sid)
        if row is None:
            return None
        user = await s.get(User, row.user_id)
        if row.expires_at <= now or not admission.admitted(user):
            await s.execute(delete(WebSession).where(WebSession.id == sid))
            await s.commit()
            return None
        if now - row.last_seen_at >= TOUCH_EVERY:
            await s.execute(update(WebSession).where(WebSession.id == sid).values(last_seen_at=now))
            await s.commit()
        assert user is not None
        return Active(token, sid, user)


async def delete_one(token: str) -> None:
    async with dbm.Session() as s:
        await s.execute(delete(WebSession).where(WebSession.id == digest(token)))
        await s.commit()


async def delete_all(user_id: int) -> int:
    async with dbm.Session() as s:
        res = await s.execute(delete(WebSession).where(WebSession.user_id == user_id))
        await s.commit()
        return res.rowcount or 0


async def count(user_id: int) -> int:
    async with dbm.Session() as s:
        return len(list(await s.scalars(select(WebSession.id).where(WebSession.user_id == user_id))))
