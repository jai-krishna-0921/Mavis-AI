"""Telegram link sign-in. The browser asks for a nonce (bound to it by an HttpOnly pre-session cookie), the
person opens https://t.me/<bot>?start=login_<nonce>[_<invite>], the bot binds the nonce to their user, and the
browser's poll turns the bound nonce into a session exactly once.

The start payload is limited to 64 characters of [A-Za-z0-9_-]: "login_" + 32 hex + "_" + "MAV" + 10 = 52."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, update

from mavis.access import admission
from mavis.access.codes import deep_link_param, normalize
from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.messages import Outbound
from mavis.store import db as dbm
from mavis.store.models import User, WebLoginNonce
from mavis.store.repo import outbox
from mavis.web import emails
from mavis.web.sessions import digest

log = structlog.get_logger(__name__)

NONCE_TTL = timedelta(minutes=10)
SIGNED_IN_TEXT = "You're signed in on the web. You can close this."
EXPIRED_TEXT = "That sign-in link expired or was already used. Go back to the website and try again."

_START = re.compile(r"^/start(?:@\w+)?\s+login_([0-9a-f]{32})(?:_([A-Za-z0-9]{1,24}))?\s*$")


@dataclass(frozen=True)
class StartLogin:
    nonce: str
    invite: str | None  # canonical invite code, when the link carried one


def parse_start(text: str) -> StartLogin | None:
    m = _START.match((text or "").strip())
    if m is None:
        return None
    invite = normalize(m.group(2)) if m.group(2) else None
    return StartLogin(m.group(1), invite)


def deep_link(nonce: str, invite: str | None = None) -> str:
    tail = f"_{deep_link_param(invite)}" if invite else ""
    bot = get_settings().telegram_bot_username or "Mavis247_bot"
    return f"https://t.me/{bot}?start=login_{nonce}{tail}"


async def start(pre_token: str, *, link_email: str | None = None) -> tuple[str, datetime]:
    """A new nonce for the browser holding `pre_token`. Returns (nonce, expires_at)."""
    nonce = secrets.token_hex(16)
    now = timeutil.now()
    expires = now + NONCE_TTL
    async with dbm.Session() as s:
        await s.execute(delete(WebLoginNonce).where(WebLoginNonce.expires_at < now))
        s.add(WebLoginNonce(nonce_hash=digest(nonce), pre_hash=digest(pre_token), link_email=link_email,
                            created_at=now, expires_at=expires))
        await s.commit()
    return nonce, expires


async def bind(nonce: str, user_id: int) -> bool:
    """The bot saw `nonce` from this user. True when it is now (or already was) bound to them."""
    now = timeutil.now()
    key = digest(nonce)
    async with dbm.Session() as s:
        user = await s.get(User, user_id)
        if not admission.admitted(user):
            return False
        row = await s.get(WebLoginNonce, key)
        if row is None or row.expires_at <= now or row.consumed_at is not None:
            return False
        if row.user_id == user_id:
            return True
        res = await s.execute(update(WebLoginNonce).where(
            WebLoginNonce.nonce_hash == key, WebLoginNonce.user_id.is_(None)).values(user_id=user_id))
        await s.commit()
        if not res.rowcount:
            return False
        link = row.link_email
    if link:
        await emails.confirm(user_id, link, "telegram_link")
    return True


async def complete(user_id: int, login: StartLogin, event_id: str) -> None:
    """Bind and tell the person in chat, plainly."""
    ok = await bind(login.nonce, user_id)
    await outbox.enqueue_now(Outbound(user_id=user_id, text=SIGNED_IN_TEXT if ok else EXPIRED_TEXT,
                                      dedupe_key=f"weblogin:{event_id}"))
    log.info("weblogin.bound" if ok else "weblogin.bind_refused", user_id=user_id)


async def collect(nonce: str, pre_token: str | None) -> tuple[str, int | None]:
    """The browser's poll. ("pending" | "ok" | "expired", user id when ok). "ok" is given once per nonce."""
    if not pre_token or not re.fullmatch(r"[0-9a-f]{32}", nonce or ""):
        return "expired", None
    now = timeutil.now()
    key = digest(nonce)
    async with dbm.Session() as s:
        row = await s.get(WebLoginNonce, key)
        if row is None or row.pre_hash != digest(pre_token) or row.consumed_at is not None \
                or row.expires_at <= now:
            return "expired", None
        if row.user_id is None:
            return "pending", None
        res = await s.execute(update(WebLoginNonce).where(
            WebLoginNonce.nonce_hash == key, WebLoginNonce.consumed_at.is_(None)).values(consumed_at=now))
        await s.commit()
        if not res.rowcount:
            return "expired", None
        user = await s.get(User, row.user_id)
        if not admission.admitted(user):
            return "expired", None
        return "ok", row.user_id


async def forget_user_nonces(user_id: int) -> None:
    async with dbm.Session() as s:
        await s.execute(delete(WebLoginNonce).where(WebLoginNonce.user_id == user_id))
        await s.commit()
