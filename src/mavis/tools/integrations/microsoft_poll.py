# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# ruff: noqa: E501
"""Poll hooks for Microsoft 365, one per capability (each has its own chain): Outlook mail and calendar deltas,
Teams chats, OneDrive shares. Every read is on the Microsoft account only (`native_call`), publishes the same
events the Gmail, Slack and Drive polls do, and keeps its cursors in `users.state["cursors"]`.

Throttling: a RATE_LIMITED or UNAVAILABLE answer sets an exponential per-user back-off (2, 4, 8, 15 minutes
at most) during which every Microsoft hook returns at once. An auth failure raises PollAuthError.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from mavis.domain import timeutil
from mavis.domain.errors import FailureKind
from mavis.domain.events import Event, EventType, Trust
from mavis.domain.integrations import ToolResult
from mavis.tools.integrations.microsoft_sync import is_external, native_call, own_tenant
from mavis.tools.integrations.native.graph_files import SHARED_UNAVAILABLE
from mavis.tools.integrations.native.providers import PollAuthError
from mavis.tools.integrations.normalize import (
    calendar_event,
    email_event,
    extract_calendar_items,
    extract_messages,
    normalize_email,
    teams_event,
    to_datetime,
)

log = structlog.get_logger()

MAIL_MAX_AGE = timedelta(hours=24)
CALENDAR_REBASELINE = timedelta(hours=20)
SHARES_EVERY = timedelta(minutes=30)
SHARES_OFF = timedelta(hours=24)
SEEN_CAP = 200
MAX_BACKOFF_MIN = 15
TEAMS_CHATS, TEAMS_PER_TICK, TEAMS_HISTORY = 30, 10, 20


def _cursors(st: dict) -> dict:
    return st.get("cursors") or {}


async def _put(poller: Any, user_id: int, st: dict, key: str, value: object) -> None:
    await poller.set_cursor(user_id, st, key, value)
    st.setdefault("cursors", {})[key] = value  # later writes in this tick build on this one


def _backed_off(poller: Any, st: dict) -> bool:
    until = to_datetime(_cursors(st).get("ms_backoff_until"))
    return until is not None and poller.clock() < until


async def _success(poller: Any, user_id: int, st: dict) -> None:
    if _cursors(st).get("ms_backoff_n"):
        await _put(poller, user_id, st, "ms_backoff_n", 0)


async def _failed(poller: Any, user_id: int, st: dict, res: ToolResult, what: str) -> int:
    """Auth ends the chain's attempt via PollAuthError; throttling backs off; anything else waits a tick."""
    log.warning("poller.microsoft_failed", user_id=user_id, what=what, kind=getattr(res.error_kind, "value", None))
    if res.error_kind is FailureKind.AUTH:
        raise PollAuthError
    if res.error_kind in (FailureKind.RATE_LIMITED, FailureKind.UNAVAILABLE):
        n = int(_cursors(st).get("ms_backoff_n") or 0) + 1
        wait = timedelta(minutes=min(2**n, MAX_BACKOFF_MIN))
        await _put(poller, user_id, st, "ms_backoff_n", n)
        await _put(poller, user_id, st, "ms_backoff_until", (poller.clock() + wait).isoformat())
    return 0


def _fresh_unread(raw: dict, now: datetime) -> bool:
    """Unread, in the inbox, not a draft or sent item, received within the last 24 hours."""
    labels = {str(x).upper() for x in raw.get("labelIds") or []}
    if "UNREAD" not in labels or labels & {"DRAFT", "SENT"}:
        return False
    received = to_datetime(normalize_email(raw)["received_at"])
    return received is not None and received >= now - MAIL_MAX_AGE


async def _publish_mail(poller: Any, user_id: int, messages: list[dict]) -> int:
    now, published = poller.clock(), 0
    for raw in messages:
        if not _fresh_unread(raw, now):
            continue
        event = email_event(user_id, raw, source="poller", prefix="outlook")
        if event is not None and await poller.bus.publish(event):
            published += 1
    return published


async def poll_mail(poller: Any, user_id: int, st: dict) -> int:
    if _backed_off(poller, st):
        return 0
    cursor = str(_cursors(st).get("outlook_delta") or "")
    res = await native_call(poller.provider, user_id, "mail.inbox_delta", {"cursor": cursor})
    if not res.ok:
        if res.error_kind is FailureKind.INVALID_ARGUMENT:
            if cursor:  # a link we cannot use: start over from now
                await _put(poller, user_id, st, "outlook_delta", "")
                return 0
            return await _mail_by_search(poller, user_id, st)
        return await _failed(poller, user_id, st, res, "mail")
    data = res.data if isinstance(res.data, dict) else {}
    published = 0
    if data.get("reset"):
        await _put(poller, user_id, st, "outlook_delta", "")
    else:
        if not data.get("baseline"):  # the first sync owns history: a baseline publishes nothing
            published = await _publish_mail(poller, user_id, extract_messages(data))
        if data.get("cursor"):  # only after the events are out: a failure above re-delivers (ids dedupe)
            await _put(poller, user_id, st, "outlook_delta", str(data["cursor"]))
    await _success(poller, user_id, st)
    return published


async def _mail_by_search(poller: Any, user_id: int, st: dict) -> int:
    """The tenant refuses the delta baseline: look at the last day every tick; event ids dedupe repeats."""
    res = await native_call(poller.provider, user_id, "mail.search",
                            {"query": "in:inbox newer_than:1d", "max_results": 25})
    if not res.ok:
        return await _failed(poller, user_id, st, res, "mail_search")
    await _success(poller, user_id, st)
    return await _publish_mail(poller, user_id, extract_messages(res.data))


async def poll_calendar(poller: Any, user_id: int, st: dict) -> int:
    if _backed_off(poller, st):
        return 0
    now = poller.clock()
    cursor = str(_cursors(st).get("ocal_delta") or "")
    at = to_datetime(_cursors(st).get("ocal_delta_at"))
    if cursor and (at is None or now - at > CALENDAR_REBASELINE):
        cursor = ""  # the window slides: take a fresh silent baseline
    res = await native_call(poller.provider, user_id, "calendar.delta", {"cursor": cursor})
    if not res.ok:
        if res.error_kind is FailureKind.INVALID_ARGUMENT:
            if cursor:
                await _put(poller, user_id, st, "ocal_delta", "")
                return 0
            return await _calendar_by_list(poller, user_id, st)
        return await _failed(poller, user_id, st, res, "calendar")
    data = res.data if isinstance(res.data, dict) else {}
    published = 0
    if data.get("reset"):
        await _put(poller, user_id, st, "ocal_delta", "")
    else:
        if not data.get("baseline"):
            for raw in extract_calendar_items(data):
                event = calendar_event(user_id, raw, source="poller", prefix="ocal")
                if event is not None and await poller.bus.publish(event):
                    published += 1
        if data.get("cursor"):
            await _put(poller, user_id, st, "ocal_delta", str(data["cursor"]))
            if not cursor or data.get("baseline"):  # the window starts with the baseline
                await _put(poller, user_id, st, "ocal_delta_at", now.isoformat())
    await _success(poller, user_id, st)
    return published


async def _calendar_by_list(poller: Any, user_id: int, st: dict) -> int:
    now = poller.clock()
    since = _cursors(st).get("ocal_updated_min") or (now - timedelta(minutes=10)).isoformat()
    res = await native_call(poller.provider, user_id, "calendar.list", {
        "time_min": now.isoformat(), "time_max": (now + timedelta(days=30)).isoformat(),
        "max_results": 50, "updated_min": since})
    if not res.ok:
        return await _failed(poller, user_id, st, res, "calendar_list")
    published = 0
    for raw in extract_calendar_items(res.data):
        event = calendar_event(user_id, raw, source="poller", prefix="ocal")
        if event is not None and await poller.bus.publish(event):
            published += 1
    await _put(poller, user_id, st, "ocal_updated_min", now.isoformat())
    await _success(poller, user_id, st)
    return published


async def poll_teams(poller: Any, user_id: int, st: dict) -> int:
    """New Teams messages. `teams_at` is the floor under every chat; `teams_chats` holds a per-chat watermark
    for chats read ahead of it. A chat that fails or is left for the next tick keeps the floor below its
    unread messages, so nothing is skipped (replays dedupe by event id)."""
    if _backed_off(poller, st):
        return 0
    now = poller.clock()
    mark = to_datetime(_cursors(st).get("teams_at"))
    res = await native_call(poller.provider, user_id, "teams.chats", {"limit": TEAMS_CHATS})
    if not res.ok:
        if res.error_kind is FailureKind.PERMISSION_MISSING:
            return 0  # no Teams licence: nothing to poll, no reconnect prompt
        return await _failed(poller, user_id, st, res, "teams_chats")
    if mark is None:  # first tick: the first sync owns history
        await _put(poller, user_id, st, "teams_at", now.isoformat())
        await _success(poller, user_id, st)
        return 0
    per_chat = {str(k): v for k, v in (_cursors(st).get("teams_chats") or {}).items()}

    def since(chat: dict) -> datetime:
        return to_datetime(per_chat.get(str(chat["id"]))) or mark

    chats = [c for c in res.data.get("chats") or [] if isinstance(c, dict) and c.get("id")]
    changed = [c for c in chats if (to_datetime(c.get("lastMessageAt")) or mark) > since(c)]
    changed.sort(key=lambda c: str(c.get("lastMessageAt") or ""))
    own_tid = await own_tenant(poller.provider, user_id)
    published, unresolved, throttled = 0, [], False
    for i, chat in enumerate(changed):
        if i >= TEAMS_PER_TICK or throttled:
            unresolved.append(chat)
            continue
        floor = since(chat)
        hist = await native_call(poller.provider, user_id, "teams.history",
                                 {"chat_id": str(chat["id"]), "limit": TEAMS_HISTORY})
        if not hist.ok:
            if hist.error_kind is FailureKind.AUTH:
                raise PollAuthError
            throttled = hist.error_kind in (FailureKind.RATE_LIMITED, FailureKind.UNAVAILABLE)
            if throttled:
                await _failed(poller, user_id, st, hist, "teams_history")
            unresolved.append(chat)  # retried next tick: its floor does not move
            continue
        members = [m for m in chat.get("members") or [] if isinstance(m, dict)]
        by_id = {str(m.get("id") or ""): m for m in members}
        roster = [{"name": str(m.get("name") or ""), "email": str(m.get("email") or ""),
                   "tenantId": str(m.get("tenantId") or ""), "me": bool(m.get("me"))} for m in members]
        for m in hist.data.get("messages") or []:
            at = to_datetime(m.get("at")) if isinstance(m, dict) else None
            if at is None or at <= floor:
                continue
            from_id, from_me = str(m.get("fromId") or ""), bool(m.get("fromMe"))
            sender = by_id.get(from_id, {})
            event = teams_event(user_id, {
                "chat_id": str(chat["id"]), "message_id": str(m.get("id") or ""),
                "from": str(m.get("from") or ""), "from_id": from_id,
                "from_address": str(sender.get("email") or ""), "from_me": from_me,
                "text": str(m.get("text") or ""), "at": str(m.get("at") or ""),
                "chat_type": str(chat.get("type") or ""), "topic": str(chat.get("topic") or ""),
                "members": roster, "external": is_external(sender.get("tenantId"), own_tid, from_me),
            }, source="poller")
            if event is not None and await poller.bus.publish(event):
                published += 1
        per_chat[str(chat["id"])] = (to_datetime(chat.get("lastMessageAt")) or floor).isoformat()
    if unresolved:
        new_floor = min(since(c) for c in unresolved)
    else:
        new_floor = max([mark, *(to_datetime(c.get("lastMessageAt")) or mark for c in chats)])
    kept = sorted(((k, v) for k, v in per_chat.items() if (to_datetime(v) or mark) > new_floor),
                  key=lambda kv: kv[1], reverse=True)[:SEEN_CAP]
    await _put(poller, user_id, st, "teams_chats", dict(kept))
    await _put(poller, user_id, st, "teams_at", new_floor.isoformat())
    if not throttled:
        await _success(poller, user_id, st)
    return published


async def poll_onedrive(poller: Any, user_id: int, st: dict) -> int:
    if _backed_off(poller, st):
        return 0
    now = poller.clock()
    due = to_datetime(_cursors(st).get("onedrive_at"))
    if due is not None and now < due + SHARES_EVERY:
        return 0
    res = await native_call(poller.provider, user_id, "onedrive.list_recent",
                            {"shared_with_me": True, "max_results": 25})
    if not res.ok:
        if res.error_kind is FailureKind.UNAVAILABLE and (res.error or "") == SHARED_UNAVAILABLE:
            # the shares endpoint is going away (uncertainty 7): off for a day, no message to the user
            await _put(poller, user_id, st, "onedrive_at", (now + SHARES_OFF - SHARES_EVERY).isoformat())
            return 0
        await _put(poller, user_id, st, "onedrive_at", now.isoformat())  # retry in 30 minutes, not 2
        return await _failed(poller, user_id, st, res, "onedrive")
    files = [f for f in res.data.get("files") or [] if isinstance(f, dict) and f.get("id")]
    seen_before = _cursors(st).get("onedrive_seen")
    seen = [str(x) for x in seen_before or []]
    published = 0
    if seen_before is not None:  # the first run only records what is already there
        for f in files:
            if str(f["id"]) in seen:
                continue
            raw = {"id": str(f["id"]), "name": str(f.get("name") or ""),
                   "sharedWithMeTime": str(f.get("sharedAt") or f.get("modifiedTime") or ""),
                   "sharingUser": {"emailAddress": str(f.get("sharedByEmail") or ""),
                                   "displayName": str(f.get("sharedBy") or "")}}
            event = _share_event(user_id, raw)
            if await poller.bus.publish(event):
                published += 1
    merged = (seen + [str(f["id"]) for f in files if str(f["id"]) not in seen])[-SEEN_CAP:]
    await _put(poller, user_id, st, "onedrive_seen", merged)
    await _put(poller, user_id, st, "onedrive_at", now.isoformat())
    await _success(poller, user_id, st)
    return published


def _share_event(user_id: int, raw: dict) -> Any:
    return Event(id=f"ods:{user_id}:{raw['id']}"[:200], user_id=user_id, type=EventType.WORKSPACE_SIGNAL,
                 occurred_at=to_datetime(raw["sharedWithMeTime"]) or timeutil.now(),
                 source="poller", payload={"kind": "onedrive_share", "raw": raw}, trust=Trust.UNTRUSTED)
