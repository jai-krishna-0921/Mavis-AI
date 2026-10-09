"""Slack Events API intake, backfill and poll safety net. All paths end in normalize.slack_event + the bus.

Webhook: verify the v0 signature over the RAW body first (nothing is parsed before that), answer the
url_verification challenge, drop replayed event ids, map each authorizing Slack user to a Mavis user and
publish one SLACK_MESSAGE event per kept message. Backfill and poll read through the provider's
`slack.channels` / `slack.history` actions, so they work with any executor behind the router.

Expected lookup (wired at merge from the native grant account data):
    async def user_for_slack(team_id: str, slack_user_id: str) -> int | None
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from mavis.bus import get_redis
from mavis.bus.base import EventBus
from mavis.domain.errors import FailureKind, IntegrationError, WebhookVerificationError
from mavis.domain.events import Event
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.base import IntegrationProvider
from mavis.tools.integrations.normalize import extract_list, slack_event

log = structlog.get_logger()

TOLERANCE_S = 300
MAX_BODY_BYTES = 1_000_000
DEDUPE_TTL_S = 86_400
# A message is kept only with no subtype or one of these; every other subtype is a system event
# (channel_join, channel_leave, channel_topic, channel_purpose, pinned_item, ...), a bot post or an edit.
KEPT_SUBTYPES = frozenset({"thread_broadcast", "file_share"})

DM_KINDS = frozenset({"dm", "group_dm"})
PER_CHANNEL_CAP = 200
TOTAL_CAP = 2000
HISTORY_PAGE = 100
POLL_BATCH = 25
POLL_PAGE = 50
POLL_INITIAL_LOOKBACK = timedelta(minutes=10)


class SlackUserLookup(Protocol):
    async def user_for_slack(self, team_id: str, slack_user_id: str) -> int | None:
        """The Mavis user who authorized this Slack user in this team, or None."""
        ...


_lookup: SlackUserLookup | None = None


def set_user_lookup(lookup: SlackUserLookup | None) -> None:
    global _lookup
    _lookup = lookup


def get_user_lookup() -> SlackUserLookup | None:
    return _lookup


# --- signature ---------------------------------------------------------------------------------------


def verify_signature(
    secret: str, headers: Mapping[str, str], body: bytes, *, tolerance_s: int = TOLERANCE_S,
    now: float | None = None,
) -> None:
    if not secret:
        raise WebhookVerificationError("SLACK_SIGNING_SECRET is not set; refusing unsigned webhooks")
    h = {k.lower(): v for k, v in headers.items()}
    ts, sig = h.get("x-slack-request-timestamp"), h.get("x-slack-signature")
    if not (ts and sig):
        raise WebhookVerificationError("missing Slack signature headers")
    try:
        ts_int = int(ts)
    except ValueError:
        raise WebhookVerificationError("malformed Slack timestamp") from None
    current = time.time() if now is None else now
    if abs(current - ts_int) > tolerance_s:
        raise WebhookVerificationError("stale Slack timestamp")
    expected = "v0=" + hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected.encode(), sig.encode("utf-8", "replace")):
        raise WebhookVerificationError("invalid Slack signature")


# --- replay ------------------------------------------------------------------------------------------


class EventDedupe:
    """Slack event ids already handled. Redis when configured, else a bounded in-process map."""

    def __init__(self, ttl_s: int = DEDUPE_TTL_S, limit: int = 10_000) -> None:
        self._ttl, self._limit = ttl_s, limit
        self._mem: dict[str, float] = {}

    async def seen(self, event_id: str) -> bool:
        client = get_redis()
        if client is not None:
            try:
                return bool(await client.exists(f"mavis:slack:ev:{event_id}"))
            except Exception:  # noqa: BLE001 - the bus dedupes on message id as well
                pass
        expiry = self._mem.get(event_id)
        return expiry is not None and expiry > time.monotonic()

    async def mark(self, event_id: str) -> None:
        client = get_redis()
        if client is not None:
            try:
                await client.set(f"mavis:slack:ev:{event_id}", "1", ex=self._ttl)
                return
            except Exception:  # noqa: BLE001
                pass
        now = time.monotonic()
        if len(self._mem) >= self._limit:
            self._mem = {k: v for k, v in self._mem.items() if v > now}
            if len(self._mem) >= self._limit:
                self._mem.clear()
        self._mem[event_id] = now + self._ttl


_dedupe = EventDedupe()


def get_dedupe() -> EventDedupe:
    return _dedupe


# --- message rules and events --------------------------------------------------------------------------


def keep_message(m: dict) -> bool:
    """Structural filter shared by webhook, backfill and poll: no bots, system subtypes or empty text."""
    if m.get("bot_id") or m.get("bot") or m.get("hidden"):
        return False
    subtype = m.get("subtype")
    if subtype and subtype not in KEPT_SUBTYPES:
        return False
    return bool(str(m.get("text") or "").strip()) and bool(m.get("ts"))


def _ts_datetime(ts: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def build_event(
    user_id: int, message: dict, channel: str, source: str, *, from_me: bool, team: str = "",
    channel_type: str = "", channel_name: str = "",
) -> Event | None:
    """Slack message JSON -> Event via normalize.slack_event, stamped with the message's own time."""
    raw = {**message, "channel": channel}
    event = slack_event(user_id, raw, source)
    if event is None:
        return None
    payload = dict(event.payload)
    name = str(message.get("from") or "").strip()
    if name:
        payload["from"] = name
    payload["from_me"] = from_me
    for key, value in (("team", team), ("channel_type", channel_type), ("channel_name", channel_name)):
        if value:
            payload[key] = value
    update: dict[str, Any] = {"payload": payload}
    when = _ts_datetime(payload["ts"])
    if when is not None:
        update["occurred_at"] = when
    return event.model_copy(update=update)


async def handle_callback(
    payload: dict, lookup: SlackUserLookup | None, bus: EventBus, dedupe: EventDedupe | None = None
) -> dict[str, int]:
    """One event_callback. Returns counts; never raises for data it does not understand."""
    dedupe = dedupe or get_dedupe()
    counts = {"published": 0, "skipped": 0, "duplicate": 0, "unmapped": 0}
    event_id = str(payload.get("event_id") or "")
    if event_id and await dedupe.seen(event_id):
        counts["duplicate"] = 1
        return counts
    ev = payload.get("event")
    if not isinstance(ev, dict) or ev.get("type") != "message":
        counts["skipped"] = 1
        return counts
    if ev.get("subtype") in {"message_changed", "message_deleted"}:
        # The pipeline stores no revisions or tombstones: log the shape and move on.
        log.info("slack.event_ignored", subtype=ev.get("subtype"), channel=ev.get("channel"))
        counts["skipped"] = 1
        if event_id:
            await dedupe.mark(event_id)
        return counts
    team = str(payload.get("team_id") or ev.get("team") or "")
    channel = str(ev.get("channel") or "")
    if not keep_message(ev) or not channel:
        counts["skipped"] = 1
        if event_id:
            await dedupe.mark(event_id)
        return counts
    grantees = [a for a in payload.get("authorizations") or []
                if isinstance(a, dict) and a.get("user_id") and not a.get("is_bot")]
    for auth in grantees:
        slack_user = str(auth["user_id"])
        auth_team = str(auth.get("team_id") or team)
        mavis_user = await lookup.user_for_slack(auth_team, slack_user) if lookup else None
        if mavis_user is None:
            counts["unmapped"] += 1
            continue
        event = build_event(
            mavis_user, ev, channel, "slack", from_me=str(ev.get("user") or "") == slack_user,
            team=team, channel_type=str(ev.get("channel_type") or ""),
        )
        if event is None:
            counts["skipped"] += 1
        elif await bus.publish(event):
            counts["published"] += 1
        else:
            counts["duplicate"] += 1
    if event_id:
        await dedupe.mark(event_id)
    return counts


async def handle_request(
    secret: str, headers: Mapping[str, str], body: bytes, lookup: SlackUserLookup | None, bus: EventBus,
    *, now: float | None = None, dedupe: EventDedupe | None = None,
) -> dict[str, Any]:
    """Verify, then parse. Returns the JSON response body; raises on a bad signature or payload."""
    if len(body) > MAX_BODY_BYTES:
        raise IntegrationError("Slack body too large")
    verify_signature(secret, headers, body, now=now)
    try:
        payload = json.loads(body)
    except ValueError:
        raise IntegrationError("Slack body is not JSON") from None
    if not isinstance(payload, dict):
        raise IntegrationError("Slack body is not a JSON object")
    kind = payload.get("type")
    if kind == "url_verification":
        return {"challenge": str(payload.get("challenge") or "")}
    if kind == "event_callback":
        return await handle_callback(payload, lookup, bus, dedupe)
    return {"ignored": str(kind or "")}


# --- backfill ----------------------------------------------------------------------------------------


def _ts_float(ts: Any) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def conversation_order(channels: list[dict]) -> list[dict]:
    """DMs and group DMs first, then the rest; each group newest `updated` first."""
    return sorted(
        channels, key=lambda c: (0 if c.get("kind") in DM_KINDS else 1, -float(c.get("updated") or 0))
    )


async def _history(provider: IntegrationProvider, user_id: int, args: dict) -> dict | None:
    res = await provider.execute(UserRef(user_id=user_id), "slack.history", args)
    if not res.ok:
        kind = res.error_kind.value if res.error_kind else ""
        log.warning("slack.history_failed", user_id=user_id, kind=kind)
        return None
    return res.data if isinstance(res.data, dict) else None


async def backfill(
    provider: IntegrationProvider, bus: EventBus, user_id: int, *, days: int, now: datetime,
    per_channel: int = PER_CHANNEL_CAP, total: int = TOTAL_CAP,
) -> int:
    """Publish the last `days` of kept messages: DMs first, then member channels by recent activity, at most
    `per_channel` kept messages per conversation and `total` overall. Returns events newly published."""
    res = await provider.execute(UserRef(user_id=user_id), "slack.channels", {})
    if not res.ok:
        log.warning("slack.backfill_channels_failed", user_id=user_id)
        return 0
    oldest = f"{(now - timedelta(days=days)).timestamp():.6f}"
    published = seen = 0
    for chan in conversation_order(extract_list(res.data, "channels", "data.channels")):
        cid = str(chan.get("id") or "")
        if not cid or seen >= total:
            continue
        kept, cursor = 0, ""
        while kept < per_channel and seen < total:
            page = await _history(provider, user_id, {
                "channel": cid, "limit": HISTORY_PAGE, "oldest": oldest, "cursor": cursor,
            })
            if page is None:
                break
            for m in page.get("messages") or []:
                if not isinstance(m, dict) or not keep_message(m) or kept >= per_channel or seen >= total:
                    continue
                kept += 1
                seen += 1
                event = build_event(
                    user_id, m, cid, "backfill", from_me=bool(m.get("from_me")),
                    channel_type=str(chan.get("kind") or ""), channel_name=str(chan.get("name") or ""),
                )
                if event is not None and await bus.publish(event):
                    published += 1
            cursor = str(page.get("next_cursor") or "")
            if not (page.get("has_more") and cursor):
                break
    return published


# --- poll safety net -----------------------------------------------------------------------------------


class PollAuthError(Exception):
    """A Slack read failed as unauthorised (so the poller can follow its reconnect path)."""


async def poll_messages(
    provider: IntegrationProvider, bus: EventBus, user_id: int, cursors: dict[str, str], offset: int, *,
    now: datetime, batch: int = POLL_BATCH,
) -> tuple[int, dict[str, str], int]:
    """Read new messages in the next `batch` conversations (round robin, DMs first) since each channel's
    latest-ts cursor. Returns (published, new cursors, next offset). A channel never seen before starts
    10 minutes back: backfill owns older history."""
    res = await provider.execute(UserRef(user_id=user_id), "slack.channels", {})
    if not res.ok:
        if res.error_kind is FailureKind.AUTH:
            raise PollAuthError
        return 0, cursors, offset
    ordered = conversation_order(extract_list(res.data, "channels", "data.channels"))
    if not ordered:
        return 0, cursors, 0
    start = offset % len(ordered)
    window = (ordered[start:] + ordered[:start])[:batch]
    out = dict(cursors)
    default_since = f"{(now - POLL_INITIAL_LOOKBACK).timestamp():.6f}"
    published = 0
    for chan in window:
        cid = str(chan.get("id") or "")
        if not cid:
            continue
        r = await provider.execute(UserRef(user_id=user_id), "slack.history", {
            "channel": cid, "limit": POLL_PAGE, "oldest": out.get(cid) or default_since,
        })
        if not r.ok:
            if r.error_kind is FailureKind.AUTH:
                raise PollAuthError
            if r.error_kind is FailureKind.RATE_LIMITED:
                break  # keep the cursors; the next tick retries
            continue
        messages = [m for m in (r.data or {}).get("messages") or [] if isinstance(m, dict)]
        for m in sorted(messages, key=lambda m: _ts_float(m.get("ts"))):
            event = build_event(
                user_id, m, cid, "poller", from_me=bool(m.get("from_me")),
                channel_type=str(chan.get("kind") or ""), channel_name=str(chan.get("name") or ""),
            ) if keep_message(m) else None
            if event is not None and await bus.publish(event):
                published += 1
        out.setdefault(cid, default_since)
        newest = max((_ts_float(m.get("ts")) for m in messages), default=0.0)
        if newest > _ts_float(out.get(cid)):
            out[cid] = max(str(m["ts"]) for m in messages if _ts_float(m.get("ts")) == newest)
    return published, out, start + len(window)
