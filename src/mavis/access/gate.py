"""The access gate (spec 4.2): decides, per event and before routing, whether a user may be served."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog

from mavis import bus
from mavis.access import UserStatus, UserTier
from mavis.access.codes import normalize
from mavis.channels import get_channel
from mavis.channels.test_sink import is_test_chat
from mavis.config import get_settings
from mavis.domain.events import Event, EventType
from mavis.domain.messages import Outbound
from mavis.store.db import utcnow
from mavis.store.models import InviteCode, User
from mavis.store.repo import audit, invites, outbox, users
from mavis.worker.gates import register_event_gate

log = structlog.get_logger(__name__)

INVITE_ONLY_TEXT = "Hi! Mavis is invite-only for now. If someone gave you an invite code, send it here."
CODE_NOT_VALID_TEXT = ("That code didn't work. Check it and send it again, or ask the person who "
                       "invited you.")
PAUSED_TEXT = "This account is paused."
TOO_MANY_TRIES_TEXT = "Too many tries, try again in an hour"
SLOW_DOWN_TEXT = "You're sending a lot at once, give me a second to catch up."

ActivatedFn = Callable[[User, InviteCode], Awaitable[None]]
on_activated: list[ActivatedFn] = []

_STATE_KEY = "access_gate"  # users.state[...]: reply timestamps, failed attempts, last activation event


def _code_in(text: str) -> str | None:
    t = (text or "").strip()
    if t.startswith("/start"):
        t = t[len("/start"):].strip()
    return normalize(t)


def _parse(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


async def _say_once(user_id: int, text: str, kind: str, window_s: float, event: Event) -> None:
    """Send `text` unless this kind of reply went out to the user in the last `window_s`. Retry safe: the
    event that recorded the send may run again and re-enqueues under the same dedupe key."""
    now = event.occurred_at
    verdict = {"send": False}

    def change(cur: dict) -> dict:
        last = _parse((cur.get("sent") or {}).get(kind))
        last_event = (cur.get("sent_event") or {}).get(kind)
        if last is not None and last_event != event.id and (now - last).total_seconds() < window_s:
            return cur
        verdict["send"] = True
        return {**cur, "sent": {**(cur.get("sent") or {}), kind: now.isoformat()},
                "sent_event": {**(cur.get("sent_event") or {}), kind: event.id}}

    await users.modify_nested(user_id, _STATE_KEY, change)
    if verdict["send"]:
        await outbox.enqueue_now(Outbound(user_id=user_id, text=text, dedupe_key=f"gate:{kind}:{event.id}"))


async def _failures(user_id: int, now: datetime, *, add: bool, event_id: str = "") -> int:
    """Failed code attempts by this user in the last hour (in the user state row). Counting is idempotent
    per event id: a retry of the same bad-code update adds nothing."""
    cutoff = now - timedelta(hours=1)
    res = {"n": 0, "added": False}

    def change(cur: dict) -> dict:
        hits = [t for t in (cur.get("fails") or []) if (p := _parse(t)) is not None and p > cutoff]
        seen = list(cur.get("fail_events") or [])
        if add and event_id not in seen:
            hits.append(now.isoformat())
            seen = (seen + [event_id])[-20:]
            res["added"] = True
        res["n"] = len(hits)
        return {**cur, "fails": hits, "fail_events": seen}

    await users.modify_nested(user_id, _STATE_KEY, change)
    if res["added"]:
        await _count_global_failure(now)
    return res["n"]


async def _count_global_failure(now: datetime) -> None:
    """Failed attempts across all chats, per hour, in Redis. Log-only signal (no lockout: a code has about 50
    bits of entropy and the per-chat limit already applies); the owner alert in Task 16 reads the counter."""
    if (client := bus.get_redis()) is None:
        return
    try:
        key = f"mavis:gate:fails:{now:%Y%m%d%H}"
        n = await client.incr(key)
        await client.expire(key, 7200)
        limit = get_settings().invite_fail_alert_per_hour
        if n == limit:  # once per hour, when the line is crossed
            log.warning("gate.global_failed_codes_high", count=n, per_hour=limit)
    except Exception as exc:  # noqa: BLE001
        log.debug("gate.fail_counter_failed", error=type(exc).__name__)


async def activate(user_id: int, invite: InviteCode, now: datetime) -> User:
    s = get_settings()
    await users.update(user_id, status=UserStatus.ACTIVE.value, tier=invite.tier, invite_id=invite.id,
                       activated_at=now, timezone=invite.default_timezone or s.default_timezone,
                       currency=invite.default_currency)
    await audit.record(user_id, actor="user", action="invite.redeemed", detail={"invite_id": invite.id})
    user = await users.get(user_id)
    for fn in list(on_activated):
        try:
            await fn(user, invite)
        except Exception as exc:  # noqa: BLE001 - onboarding trouble must not undo the activation
            log.warning("gate.on_activated_failed", error=type(exc).__name__)
    return user


async def _pending(user: User, event: Event) -> bool:
    s = get_settings()
    now = event.occurred_at
    if event.type is not EventType.USER_MESSAGE:
        return False
    code = _code_in(str(event.payload.get("text", "")))
    if code is None:
        await _say_once(user.id, INVITE_ONLY_TEXT, "invite_only", s.pending_reply_every_h * 3600, event)
        return False
    if await _failures(user.id, now, add=False) >= s.invite_fail_limit_per_hour:
        await _say_once(user.id, TOO_MANY_TRIES_TEXT, "too_many", 3600, event)
        return False
    invite = await invites.redeem(code, user.id, now)
    if invite is None:
        await _failures(user.id, now, add=True, event_id=event.id)
        await outbox.enqueue_now(Outbound(user_id=user.id, text=CODE_NOT_VALID_TEXT,
                                          dedupe_key=f"gate:badcode:{event.id}"))
        return False
    await users.modify_nested(user.id, _STATE_KEY, lambda cur: {**cur, "activation_event": event.id})
    await activate(user.id, invite, now)
    return False  # the redemption message itself is not a chat turn; onboarding takes it from here


async def _membership(event: Event) -> bool:
    p = event.payload
    chat_id, chat_type, status = p.get("chat_id"), p.get("chat_type"), p.get("status")
    if chat_id is None:
        return False
    if chat_type != "private" and status in ("member", "administrator"):
        try:
            await get_channel().leave_chat(int(chat_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("gate.leave_chat_failed", error=type(exc).__name__)
    elif chat_type == "private" and (u := await users.get_by_chat(int(chat_id))) is not None:
        await users.update(u.id, inactive_since=utcnow() if status == "kicked" else None)
    return False


async def _grandfather_owner(user: User, now: datetime) -> User:
    """A chat listed in OWNER_TELEGRAM_CHAT_IDS is the owner, whatever its row says: active, tier owner.
    This is what keeps the existing allowlisted owner in when the invite gate is switched on."""
    if user.status == UserStatus.ACTIVE and user.tier == UserTier.OWNER:
        return user
    await users.update(user.id, status=UserStatus.ACTIVE.value, tier=UserTier.OWNER.value,
                       activated_at=user.activated_at or now, banned_at=None, ban_reason=None,
                       deleted_at=None)
    await audit.record(user.id, actor="system", action="owner.grandfathered", detail={})
    return await users.get(user.id)


async def access_gate(event: Event) -> bool:
    s = get_settings()
    if event.type is EventType.CHAT_MEMBER:
        return await _membership(event)
    if event.user_id == 0:
        return True
    user = await users.get(event.user_id)
    if event.type is EventType.RATE_LIMITED:
        await _say_once(user.id, SLOW_DOWN_TEXT, "slow_down", 60, event)
        return False
    if s.access_mode == "allowlist" or is_test_chat(user.telegram_chat_id, s):
        return True
    if user.telegram_chat_id in s.owner_telegram_chat_ids:
        if s.access_mode == "invite":
            await _grandfather_owner(user, event.occurred_at)
        return True
    status = user.status
    if s.access_mode == "shadow":
        if status != UserStatus.ACTIVE:
            log.info("gate.shadow_would_drop", status=status, event_type=event.type)
        return True
    if status == UserStatus.ACTIVE:
        if (event.type is EventType.USER_MESSAGE
                and (user.state or {}).get(_STATE_KEY, {}).get("activation_event") == event.id):
            return False  # a retry of the very message that redeemed the code is not a chat turn
        return True
    if status == UserStatus.DELETED and event.type is EventType.USER_MESSAGE:
        await users.update(user.id, status=UserStatus.PENDING.value, deleted_at=None)
        user = await users.get(user.id)
        status = UserStatus.PENDING
    if status == UserStatus.PENDING:
        return await _pending(user, event)
    if status == UserStatus.BANNED and event.type is EventType.USER_MESSAGE:
        await _say_once(user.id, PAUSED_TEXT, "paused", 10 * 365 * 86400, event)
    return False


async def purge_strangers() -> int:
    """Drop pending rows older than PENDING_RETENTION_DAYS (they never held content). Runs at worker start."""
    s = get_settings()
    n = await users.purge_strangers(utcnow() - timedelta(days=s.pending_retention_days),
                                    frozenset(s.owner_telegram_chat_ids))
    if n:
        log.info("gate.strangers_purged", count=n)
    return n


def register_access_gate() -> None:
    from mavis.worker.runner import register_startup_hook

    register_event_gate("access", access_gate, order=10)
    register_startup_hook(purge_strangers)
