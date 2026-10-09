"""Telegram link sign-in. The browser asks for a nonce (bound to it by an HttpOnly pre-session cookie), the
person opens https://t.me/<bot>?start=login_<nonce>[_<invite>] and the bot asks them to confirm (what is
asking, and a code that the browser also shows). Only the Approve button binds the nonce to their user,
and the browser's poll turns the bound nonce into a session exactly once.

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
from mavis.domain.events import Event
from mavis.domain.messages import Button, Outbound
from mavis.store import db as dbm
from mavis.store.models import User, WebLoginNonce, WebPendingLink
from mavis.store.repo import outbox
from mavis.web import emails
from mavis.web.sessions import digest

log = structlog.get_logger(__name__)

NONCE_TTL = timedelta(minutes=10)
LINK_TTL = timedelta(minutes=15)
APPROVE_PREFIX = "wl:"  # wl:y:<nonce> / wl:n:<nonce>: 5 + 32 characters, inside Telegram's 64 byte limit
SIGNED_IN_TEXT = "You're signed in on the web. You can close this."
CANCELLED_TEXT = "Okay, that sign in request was cancelled."
EXPIRED_TEXT = "That sign-in link expired or was already used. Go back to the website and try again."

_BUTTON = re.compile(r"wl:([yn]):([0-9a-f]{32})")
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


def describe_agent(user_agent: str) -> str:
    """"Chrome on Linux" from a User-Agent header. Coarse on purpose: it is shown to a person to recognise."""
    ua = user_agent or ""
    browser = next((name for needle, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                              ("Chrome/", "Chrome"), ("Safari/", "Safari")) if needle in ua),
                   "An unknown browser")
    systems = (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iOS"),
               ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
    system = next((name for needle, name in systems if needle in ua), "")
    return f"{browser} on {system}" if system else browser


def mask_ip(ip: str) -> str:
    """The network part of an address only: 203.0.x.x or 2001:db8:x:x."""
    ip = (ip or "").strip()
    if "." in ip and ip.count(".") == 3:
        a, b, *_ = ip.split(".")
        return f"{a}.{b}.x.x"
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:2]) + ":x:x"
    return "unknown"


def describe_place(ip: str, country: str = "") -> str:
    """A country code when the edge gave one (cheap, no lookup), else the masked address."""
    country = (country or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", country) and country not in {"XX", "T1"}:
        return f"{country} (near {mask_ip(ip)})"
    return mask_ip(ip)


def spaced(code: str) -> str:
    return " ".join(code)


async def start(pre_token: str, *, link_email: str | None = None, user_agent: str = "", ip: str = "",
                country: str = "") -> tuple[str, datetime, str]:
    """A new nonce for the browser holding `pre_token`. Returns (nonce, expires_at, code). The code is shown
    in that browser and in the bot's confirmation, so the person can tell the request is theirs."""
    nonce = secrets.token_hex(16)
    code = f"{secrets.randbelow(10_000):04d}"
    now = timeutil.now()
    expires = now + NONCE_TTL
    async with dbm.Session() as s:
        await s.execute(delete(WebLoginNonce).where(WebLoginNonce.expires_at < now))
        s.add(WebLoginNonce(nonce_hash=digest(nonce), pre_hash=digest(pre_token), link_email=link_email,
                            created_at=now, expires_at=expires, code=code,
                            agent_hint=describe_agent(user_agent)[:80],
                            place_hint=describe_place(ip, country)[:64]))
        await s.commit()
    return nonce, expires, code


async def stash_link(pre_token: str, email: str) -> str:
    """Keep a Google address server-side until it is linked to a Telegram account. Returns the opaque id for
    the URL, which means nothing without the cookie that holds `pre_token`."""
    link_id = secrets.token_urlsafe(16)
    now = timeutil.now()
    async with dbm.Session() as s:
        await s.execute(delete(WebPendingLink).where(WebPendingLink.expires_at < now))
        s.add(WebPendingLink(id_hash=digest(link_id), pre_hash=digest(pre_token), email=emails.norm(email),
                             created_at=now, expires_at=now + LINK_TTL))
        await s.commit()
    return link_id


async def pending_link_email(link_id: str | None, pre_token: str | None) -> str | None:
    """The address held for this browser, or None (unknown, expired, spent, or another browser's)."""
    if not link_id or not pre_token or len(link_id) > 64:
        return None
    async with dbm.Session() as s:
        row = await s.get(WebPendingLink, digest(link_id))
        if row is None or row.pre_hash != digest(pre_token) or row.expires_at <= timeutil.now():
            return None
        return row.email


async def _approval_text(row: WebLoginNonce) -> str:
    when = timeutil.ensure_utc(row.created_at).strftime("%d %b %Y, %H:%M UTC")
    lines = ["Someone asked to sign in to Mavis AI on the web.", "",
             f"Browser: {row.agent_hint or 'unknown'}", f"Where: {row.place_hint or 'unknown'}",
             f"When: {when}", ""]
    if row.link_email:
        lines += [f"Approving also links the Google address {row.link_email} to your Mavis account.", ""]
    lines += [f"Check that the code matches the one on the sign in page: {spaced(row.code)}", "",
              "If it matches and this is you, tap Approve. If you did not ask for this, tap Not me."]
    return "\n".join(lines)


async def request_approval(user_id: int, login: StartLogin, event_id: str) -> None:
    """The bot saw `/start login_<nonce>`. Pressing Start never signs anyone in: ask the person to confirm
    what is asking, and only the Approve button binds the nonce."""
    now = timeutil.now()
    key = digest(login.nonce)
    async with dbm.Session() as s:
        user = await s.get(User, user_id)
        row = await s.get(WebLoginNonce, key)
        live = (row is not None and row.expires_at > now and row.consumed_at is None and row.user_id is None
                and admission.admitted(user))
        if live:
            await s.execute(update(WebLoginNonce).where(WebLoginNonce.nonce_hash == key)
                            .values(pending_user_id=user_id))
            await s.commit()
    if not live or row is None:
        await outbox.enqueue_now(Outbound(user_id=user_id, text=EXPIRED_TEXT,
                                          dedupe_key=f"weblogin:{event_id}"))
        log.info("weblogin.request_refused", user_id=user_id)
        return
    await outbox.enqueue_now(Outbound(
        user_id=user_id, text=await _approval_text(row), dedupe_key=f"weblogin:{event_id}",
        buttons=[[Button(label="Approve", data=f"{APPROVE_PREFIX}y:{login.nonce}"),
                  Button(label="Not me", data=f"{APPROVE_PREFIX}n:{login.nonce}")]]))
    log.info("weblogin.approval_asked", user_id=user_id)


async def approve(nonce: str, user_id: int) -> bool:
    """The Approve button. True only for the person the confirmation was sent to, once, before it expires."""
    now = timeutil.now()
    key = digest(nonce)
    async with dbm.Session() as s:
        user = await s.get(User, user_id)
        if not admission.admitted(user):
            return False
        row = await s.get(WebLoginNonce, key)
        if (row is None or row.expires_at <= now or row.consumed_at is not None
                or row.pending_user_id != user_id):
            return False
        res = await s.execute(update(WebLoginNonce).where(
            WebLoginNonce.nonce_hash == key, WebLoginNonce.user_id.is_(None),
            WebLoginNonce.pending_user_id == user_id).values(user_id=user_id))
        if not res.rowcount:
            return False
        link = row.link_email
        if link:  # the address is spent with the approval: it cannot be linked again from the same page
            await s.execute(delete(WebPendingLink).where(WebPendingLink.email == link))
        await s.commit()
    if link:
        await emails.confirm(user_id, link, "telegram_link")
    return True


async def reject(nonce: str, user_id: int) -> bool:
    """The Not me button: the nonce dies, so nobody can approve or collect it."""
    async with dbm.Session() as s:
        user = await s.get(User, user_id)
        if not admission.admitted(user):
            return False
        res = await s.execute(delete(WebLoginNonce).where(
            WebLoginNonce.nonce_hash == digest(nonce), WebLoginNonce.consumed_at.is_(None)))
        await s.commit()
        return bool(res.rowcount)


async def on_button(event: Event, data: str) -> None:
    m = _BUTTON.fullmatch(data)
    if m is None:
        return
    yes, nonce = m.group(1) == "y", m.group(2)
    ok = await (approve(nonce, event.user_id) if yes else reject(nonce, event.user_id))
    text = (SIGNED_IN_TEXT if yes else CANCELLED_TEXT) if ok else EXPIRED_TEXT
    await outbox.enqueue_now(Outbound(user_id=event.user_id, text=text,
                                      dedupe_key=f"weblogin:btn:{event.id}"))
    log.info("weblogin.approved" if ok and yes else "weblogin.rejected" if ok else "weblogin.button_refused",
             user_id=event.user_id)


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
