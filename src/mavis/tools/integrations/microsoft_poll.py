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
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.graph import NO_MAILBOX
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
TEAMS_PAGES = 3  # history pages read per chat per tick; a longer backlog continues next tick
RESUME_CAP = 50


def _cursors(st: dict) -> dict:
    return st.get("cursors") or {}


async def _put(poller: Any, user_id: int, st: dict, key: str, value: object) -> None:
    grant = getattr(poller.provider, "provider_grant", None)
    if grant is not None and await grant(user_id, NativeProvider.MICROSOFT) is None:
        return  # disconnected while this tick ran: a cleared provider's cursors are not written back
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


MAILBOX_REST = timedelta(hours=6)


async def _no_mailbox(poller: Any, user_id: int, st: dict, res: ToolResult) -> bool:
    """A work account with no Exchange mailbox: mail rests for hours on its own clock. It is not an outage,
    so the shared back-off that Teams, OneDrive and the calendar obey stays untouched."""
    if res.error_kind is not FailureKind.UNAVAILABLE or NO_MAILBOX not in (res.error or ""):
        return False
    await _put(poller, user_id, st, "outlook_off_until", (poller.clock() + MAILBOX_REST).isoformat())
    return True


async def poll_mail(poller: Any, user_id: int, st: dict) -> int:
    if _backed_off(poller, st):
        return 0
    off = to_datetime(_cursors(st).get("outlook_off_until"))
    if off is not None and poller.clock() < off:
        return 0
    cursor = str(_cursors(st).get("outlook_delta") or "")
    res = await native_call(poller.provider, user_id, "mail.inbox_delta", {"cursor": cursor})
    if not res.ok:
        if res.error_kind is FailureKind.INVALID_ARGUMENT:
            if cursor:  # a link we cannot use: start over from now
                await _put(poller, user_id, st, "outlook_delta", "")
                await _put(poller, user_id, st, "outlook_baselining", False)
                return 0
            return await _mail_by_search(poller, user_id, st)
        if await _no_mailbox(poller, user_id, st, res):
            return 0
        return await _failed(poller, user_id, st, res, "mail")
    data = res.data if isinstance(res.data, dict) else {}
    published = 0
    if data.get("reset"):
        await _put(poller, user_id, st, "outlook_delta", "")
        await _put(poller, user_id, st, "outlook_baselining", False)
    else:
        # the first sync owns history: a baseline publishes nothing, and neither does the rest of a long one
        # (its next link is stored with `outlook_baselining` until the delta link arrives)
        silent = bool(data.get("baseline")) or bool(_cursors(st).get("outlook_baselining"))
        if not silent:
            published = await _publish_mail(poller, user_id, extract_messages(data))
        if data.get("cursor"):  # only after the events are out: a failure above re-delivers (ids dedupe)
            await _put(poller, user_id, st, "outlook_delta", str(data["cursor"]))
            baselining = silent and bool(data.get("more"))
            if baselining != bool(_cursors(st).get("outlook_baselining")):
                await _put(poller, user_id, st, "outlook_baselining", baselining)
    await _success(poller, user_id, st)
    return published


async def _mail_by_search(poller: Any, user_id: int, st: dict) -> int:
    """The tenant refuses the delta baseline: look at the last day every tick; event ids dedupe repeats."""
    res = await native_call(poller.provider, user_id, "mail.search",
                            {"query": "in:inbox newer_than:1d", "max_results": 25})
    if not res.ok:
        if await _no_mailbox(poller, user_id, st, res):
            return 0
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


RESUME_TTL = timedelta(hours=24)


def _expired(rec: dict, now: datetime) -> bool:
    made = to_datetime(rec.get("at"))
    return made is not None and now - made > RESUME_TTL


def _before(page: list[dict], nxt: str, bound: datetime | None) -> bool:
    """True once a page reaches `bound` (a message at or before it, or the oldest message read is)."""
    oldest = to_datetime(nxt) if nxt else None
    if not nxt or bound is None:
        return True
    if oldest is not None and oldest <= bound:
        return True
    # a message whose time cannot be read says nothing about how far down the page got
    return any((at := to_datetime(m.get("at"))) is not None and at <= bound for m in page)


async def _read_chat(poller: Any, user_id: int, cid: str, floor: datetime, resume: dict | None
                     ) -> tuple[list[dict], dict | None] | ToolResult:
    """New messages of one chat, newest first by page, down to `floor` (at most TEAMS_PAGES pages).
    Returns (messages newer than the floor, None) when it got all the way down, or (messages, resume) when
    the backlog is longer: the resume record holds where the older part continues (`before`) and the newest
    message already published (`top`), so the next tick reads what arrived since, then goes on downward.
    A failed read returns its ToolResult."""
    top = to_datetime((resume or {}).get("top"))
    out: list[dict] = []
    pages, newest = 0, top
    # 1) from the newest message down to what was read before (everything on the first pass)
    bound = top or floor
    before, reached = "", False
    while pages < TEAMS_PAGES:
        args: dict[str, Any] = {"chat_id": cid, "limit": TEAMS_HISTORY}
        if before:
            args["before"] = before
        hist = await native_call(poller.provider, user_id, "teams.history", args)
        pages += 1
        if not hist.ok or not isinstance(hist.data, dict):
            return hist if not hist.ok else ToolResult(ok=False, error_kind=FailureKind.UNKNOWN, error="shape")
        page = [m for m in hist.data.get("messages") or [] if isinstance(m, dict)]
        nxt = str(hist.data.get("next") or "")
        for m in page:
            at = to_datetime(m.get("at"))
            if at is not None and at > bound:
                out.append(m)
                newest = at if newest is None or at > newest else newest
        if _before(page, nxt, bound) or nxt == before:
            reached = True
            break
        before = nxt
    if resume is None:
        if reached:
            return out, None
        return out, {"before": before, "top": (newest or floor).isoformat()}
    # 2) resuming: go on downward from where the last tick stopped, with the pages left
    below, done = str(resume.get("before") or ""), False
    while pages < TEAMS_PAGES + 1 and below:
        hist = await native_call(poller.provider, user_id, "teams.history",
                                 {"chat_id": cid, "limit": TEAMS_HISTORY, "before": below})
        pages += 1
        if not hist.ok or not isinstance(hist.data, dict):
            return hist if not hist.ok else ToolResult(ok=False, error_kind=FailureKind.UNKNOWN, error="shape")
        page = [m for m in hist.data.get("messages") or [] if isinstance(m, dict)]
        nxt = str(hist.data.get("next") or "")
        out += [m for m in page if (to_datetime(m.get("at")) or floor) > floor]
        if _before(page, nxt, floor) or nxt == below:
            done = True
            break
        below = nxt
    if done and reached:
        return out, None
    return out, {"before": below, "top": (newest or floor).isoformat() if reached else str(resume.get("top") or "")}


async def poll_teams(poller: Any, user_id: int, st: dict) -> int:
    """New Teams messages. `teams_at` is the floor under every chat; `teams_chats` holds a per-chat watermark
    for chats read ahead of it. History is paged newest first down to the chat's floor. A chat that fails, or
    whose backlog is longer than TEAMS_PAGES pages, keeps the floor below its unread messages (the backlog
    continues from `teams_resume` next tick), so nothing is skipped (replays dedupe by event id)."""
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
    listed = {str(c.get("id")) for c in chats}
    resume = {str(k): v for k, v in (_cursors(st).get("teams_resume") or {}).items()
              if isinstance(v, dict) and not _expired(v, now)}  # a chat missing from this list keeps its entry
    published, unresolved, throttled = 0, [], False
    for i, chat in enumerate(changed):
        if i >= TEAMS_PER_TICK or throttled:
            unresolved.append(chat)
            continue
        floor, cid = since(chat), str(chat["id"])
        got = await _read_chat(poller, user_id, cid, floor, resume.get(cid))
        if isinstance(got, ToolResult):
            if got.error_kind is FailureKind.AUTH:
                raise PollAuthError
            throttled = got.error_kind in (FailureKind.RATE_LIMITED, FailureKind.UNAVAILABLE)
            if throttled:
                await _failed(poller, user_id, st, got, "teams_history")
            unresolved.append(chat)  # retried next tick: its floor does not move
            continue
        messages, left = got
        members = [m for m in chat.get("members") or [] if isinstance(m, dict)]
        by_id = {str(m.get("id") or ""): m for m in members}
        roster = [{"name": str(m.get("name") or ""), "email": str(m.get("email") or ""),
                   "tenantId": str(m.get("tenantId") or ""), "me": bool(m.get("me"))} for m in members]
        for m in sorted(messages, key=lambda x: to_datetime(x.get("at")) or floor):  # oldest first
            from_id, from_me = str(m.get("fromId") or ""), bool(m.get("fromMe"))
            sender = by_id.get(from_id, {})
            event = teams_event(user_id, {
                "chat_id": cid, "message_id": str(m.get("id") or ""),
                "from": str(m.get("from") or ""), "from_id": from_id,
                "from_address": str(sender.get("email") or ""), "from_me": from_me,
                "text": str(m.get("text") or ""), "at": str(m.get("at") or ""),
                "chat_type": str(chat.get("type") or ""), "topic": str(chat.get("topic") or ""),
                "members": roster, "external": is_external(sender.get("tenantId"), own_tid, from_me),
            }, source="poller")
            if event is not None and await poller.bus.publish(event):
                published += 1
        if left is not None:  # older messages remain: the floor stays below this chat, the next tick continues
            old = resume.get(cid) or {}
            resume[cid] = {**left, "at": old.get("at") or now.isoformat(), "floor": old.get("floor") or floor.isoformat()}
            unresolved.append(chat)
            continue
        resume.pop(cid, None)
        per_chat[cid] = (to_datetime(chat.get("lastMessageAt")) or floor).isoformat()
    if unresolved:
        new_floor = min(since(c) for c in unresolved)
    else:
        new_floor = max([mark, *(to_datetime(c.get("lastMessageAt")) or mark for c in chats)])
    for cid, rec in resume.items():
        if cid not in listed:  # its older part is still owed: the floor stays below it until it expires
            new_floor = min(new_floor, to_datetime(rec.get("floor")) or mark)
    if res.data.get("unordered") and chats:  # a plain page of chats: those outside it were never seen
        oldest_seen = min(to_datetime(c.get("lastMessageAt")) or mark for c in chats)
        new_floor = min(new_floor, max(oldest_seen, mark))
    kept = sorted(((k, v) for k, v in per_chat.items() if (to_datetime(v) or mark) > new_floor),
                  key=lambda kv: kv[1], reverse=True)[:SEEN_CAP]
    await _put(poller, user_id, st, "teams_chats", dict(kept))
    await _put(poller, user_id, st, "teams_resume", dict(list(resume.items())[-RESUME_CAP:]))
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
