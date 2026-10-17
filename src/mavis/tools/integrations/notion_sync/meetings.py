# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""The Meetings pass: one Notion page per meeting, Prep before it, Fathom notes and the user's own after it.

Pages come from qualifying calendar events (last 7 days to next 14) plus Fathom-only recordings. Mavis owns
the blocks it writes (their ids live in the link's `meta["blocks"]`); everything below the divider is the
user's. Prep is frozen once the meeting has started (kept in `meta["prep"]`). The same pass asks, once, how a
meeting without notes went (`ask_due`). Link kind `meeting`, local key = the meeting's event key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from mavis.domain import timeutil
from mavis.domain.decisions import NotifyIntent
from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.memory.names import normalize_name
from mavis.memory.service import get_memory
from mavis.store.repo import loops as loop_repo
from mavis.store.repo import meeting_notes, notion_links, users
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.notion_api import NotionError
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.normalize import extract_calendar_items, normalize_calendar_event, to_datetime
from mavis.tools.integrations.notion_sync import blocks
from mavis.tools.integrations.notion_sync.blocks import TEXT_LIMIT, content_hash
from mavis.tools.integrations.notion_sync.runner import SyncCtx, _cursors, register_pass

log = structlog.get_logger(__name__)

LINK = "meeting"
PERSON_LINK = "person"
BACK = timedelta(days=7)
AHEAD = timedelta(days=14)
ASK_WITHIN = timedelta(hours=6)
WAIT_WITH_FATHOM = timedelta(minutes=90)
WAIT_WITHOUT = timedelta(minutes=15)
ASKS_PER_PASS = 2
FACTS_PER_PERSON = 3
MAX_LOOPS = 5
MAX_PEOPLE = 10


# ----- calendar

async def calendar_events(user_id: int, start: datetime, end: datetime) -> list[dict]:
    res = await get_provider().execute(
        UserRef(user_id=user_id), "calendar.list",
        {"time_min": start.isoformat(), "time_max": end.isoformat(), "max_results": 100})
    return extract_calendar_items(res.data) if res.ok else []


async def fathom_grant(user_id: int) -> Grant | None:
    tokens = getattr(get_provider(), "tokens", None)
    return await tokens.grant(user_id, NativeProvider.FATHOM) if tokens is not None else None


def qualifies(event: dict) -> bool:
    """A real meeting: timed, not cancelled, not declined by the user, with someone else invited."""
    if "date" in (event.get("start") or {}):
        return False
    if str(event.get("status") or "") == "cancelled":
        return False
    attendees = [a for a in event.get("attendees") or [] if isinstance(a, dict)]
    mine = [a for a in attendees if a.get("self")]
    if any(a.get("responseStatus") == "declined" for a in mine):
        return False
    return any(not a.get("self") for a in attendees)


@dataclass
class Meeting:
    key: str
    title: str
    start: datetime
    end: datetime
    attendees: list[dict] = field(default_factory=list)  # {"name", "email"}, never the user
    row: meeting_notes.MeetingNoteRow | None = None


def from_event(raw: dict) -> Meeting | None:
    key = str(raw.get("id") or "")
    ev = normalize_calendar_event(raw)
    start = to_datetime(ev["start"])
    if not key or start is None:
        return None
    start = timeutil.ensure_utc(start)
    end = timeutil.ensure_utc(to_datetime(ev["end"]) or start)
    people = [{"name": str(a.get("displayName") or ""), "email": str(a.get("email") or "").lower()}
              for a in raw.get("attendees") or [] if isinstance(a, dict) and not a.get("self")]
    return Meeting(key, ev["summary"], start, max(end, start), people)


def _from_row(row: meeting_notes.MeetingNoteRow) -> Meeting | None:
    if row.starts_at is None:
        return None
    people = [{"name": str(a.get("name") or ""), "email": str(a.get("email") or "").lower()}
              for a in row.attendees if isinstance(a, dict)]
    return Meeting(row.event_key, row.title or "Meeting", row.starts_at,
                   max(row.ends_at or row.starts_at, row.starts_at), people, row)


# ----- rendering

def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}] if text else []


def _properties(m: Meeting, person_ids: list[str]) -> dict:
    when = {"start": m.start.isoformat()}
    if m.end > m.start:
        when["end"] = m.end.isoformat()
    row = m.row
    return {
        "Name": {"title": _rich(m.title)},
        "Mavis key": {"rich_text": _rich(f"meeting:{m.key}")},
        "When": {"date": when},
        "People": {"relation": [{"id": i} for i in person_ids]},
        "Recording": {"url": (row.fathom_url or None) if row else None},
    }


def _label(person: dict, link) -> str:
    return (link.meta.get("name") if link else "") or person["name"] or person["email"].split("@")[0]


async def _prep(uid: int, m: Meeting, links: dict) -> list[dict]:
    people = [(p, links.get(p["email"])) for p in m.attendees]
    names = [_label(p, link) for p, link in people]
    items = [f"With: {', '.join(n for n in names if n)}"] if any(names) else []
    graph = get_memory().graph
    for (_, link), name in list(zip(people, names, strict=True))[:MAX_PEOPLE]:
        if link is None or not name:
            continue
        for fact in await graph.neighborhood(uid, [name], hops=1, limit=FACTS_PER_PERSON):
            items.append(f"{name}: {fact}")
    wanted = {normalize_name(n) for n in names if n}
    open_loops = [lp for lp in await loop_repo.list_live(uid)
                  if wanted & {normalize_name(e) for e in lp.entities}]
    items += [f"Open: {lp.title}" for lp in open_loops[:MAX_LOOPS]]
    return [blocks.heading("Prep"), *blocks.bullets(items)]


def _notes(m: Meeting) -> list[dict]:
    row, out = m.row, []
    if row is not None and (row.summary or row.action_items):
        out.append(blocks.heading("From Fathom"))
        if row.summary:
            out += blocks.para(row.summary)
        texts = []
        for item in row.action_items:
            text = str(item.get("description") or "") if isinstance(item, dict) else str(item)
            who = str(item.get("assignee_name") or "") if isinstance(item, dict) else ""
            if text:
                texts.append(f"{text} ({who})" if who else text)
        if texts:
            out += [blocks.heading("Action items", 3), *blocks.bullets(texts)]
    if row is not None and row.user_notes:
        out += [blocks.heading("From you"), *blocks.para(row.user_notes)]
    return out


async def _sync_one(ctx: SyncCtx, m: Meeting, links: dict) -> None:
    uid = ctx.user_id
    link = await notion_links.get(uid, LINK, m.key)
    person_ids = sorted({links[p["email"]].page_id for p in m.attendees if p["email"] in links})
    props = _properties(m, person_ids)
    if link is not None and link.meta.get("prep") and m.start <= ctx.now:
        prep = link.meta["prep"]  # started: Prep stays as it was
    else:
        prep = await _prep(uid, m, links)
    owned = [*prep, *_notes(m)]
    digest = content_hash({"props": props, "body": owned})
    if link is not None and link.pushed_hash == digest:
        return
    page_id = link.page_id if link is not None else ""
    fresh = link is None or bool(link.meta.get("fresh"))
    if link is not None:
        try:
            await ctx.api.update_page(uid, page_id, properties=props)
        except NotionError as exc:
            if exc.kind not in (FailureKind.NOT_FOUND, FailureKind.INVALID_ARGUMENT):
                raise
            await notion_links.delete(uid, LINK, m.key)  # the page is gone: make a new one
            link, fresh = None, True
    if link is None:
        page = await ctx.api.create_page(
            uid, {"type": "data_source_id", "data_source_id": ctx.home.ds["meetings"]}, props)
        page_id = page["id"]
        # link first: if the body write fails, the retry finishes this page instead of making another
        await notion_links.upsert(uid, LINK, m.key, page_id, pushed_hash="", meta={"fresh": True})
    if fresh:
        ids = await blocks.init_page_body(ctx.api, uid, page_id, owned)
    else:
        ids = await blocks.replace_owned(ctx.api, uid, page_id, list(link.meta.get("blocks") or []), owned)
    await notion_links.upsert(uid, LINK, m.key, page_id, pushed_hash=digest, pushed_at=timeutil.now(),
                              meta={"blocks": ids, "prep": prep})


# ----- the ask

async def _ask_one(ctx: SyncCtx, m: Meeting) -> None:
    from mavis.initiative import wiring

    uid = ctx.user_id
    user = await users.get(uid)
    tz = user.timezone
    when = timeutil.to_local(m.start, tz).strftime("%a %H:%M")
    names = [p["name"] or p["email"].split("@")[0] for p in m.attendees][:2]
    with_ = f" with {' and '.join(names)}" if names else ""
    title = " ".join(m.title.split())[:100]
    await wiring.current().executor.notify(user, NotifyIntent(
        urgency=2, dedupe_key=f"meeting_ask:{m.key}",
        intent=f'Ask the user in one short line how their meeting "{title}" ({when}{with_}) went, '
               f"so Mavis AI can note it down, and say they can skip it."), untrusted=True)
    await meeting_notes.upsert(uid, m.key, title=m.title, starts_at=m.start, ends_at=m.end,
                               attendees=m.attendees)
    await meeting_notes.mark_asked(uid, m.key, ctx.now)
    await users.update_state(uid, {"meeting_ask": {"event_key": m.key, "asked_at": ctx.now.isoformat()}})


async def ask_due(ctx: SyncCtx, events: list[dict] | None = None) -> None:
    uid = ctx.user_id
    if (await _cursors(uid)).get("notion_setup") != "done":
        return
    grant = await fathom_grant(uid)
    wait = WAIT_WITH_FATHOM if grant is not None and grant.status == ACTIVE else WAIT_WITHOUT
    if events is None:
        events = await calendar_events(uid, ctx.now - BACK, ctx.now + AHEAD)
    due = []
    for raw in events:
        m = from_event(raw) if qualifies(raw) else None
        if m is None or not (ctx.now - ASK_WITHIN <= m.end <= ctx.now - wait):
            continue
        row = await meeting_notes.get(uid, m.key)
        if row is not None and (row.recording_id or row.user_notes or row.asked_at):
            continue
        due.append(m)
    for m in sorted(due, key=lambda x: x.end, reverse=True)[:ASKS_PER_PASS]:
        await _ask_one(ctx, m)


# ----- the pass

async def meetings_pass(ctx: SyncCtx) -> None:
    uid = ctx.user_id
    events = await calendar_events(uid, ctx.now - BACK, ctx.now + AHEAD)
    found: dict[str, Meeting] = {}
    for raw in events:
        m = from_event(raw) if qualifies(raw) else None
        if m is not None:
            m.row = await meeting_notes.get(uid, m.key)
            found[m.key] = m
    for row in await meeting_notes.in_window(uid, ctx.now - BACK, ctx.now + AHEAD):
        m = _from_row(row) if row.event_key.startswith("fathom:") else None
        if m is not None:
            found[m.key] = m
    links = {str(r.meta.get("email") or "").lower(): r for r in await notion_links.all_of(uid, PERSON_LINK)
             if r.meta.get("email")}
    for m in sorted(found.values(), key=lambda x: x.start):
        if not ctx.left():
            return
        await _sync_one(ctx, m, links)
    if ctx.left():
        await ask_due(ctx, events)


register_pass("meetings", timedelta(minutes=60), meetings_pass, order=30)
