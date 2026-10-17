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
from mavis.domain.integrations import UserRef
from mavis.memory.names import normalize_name
from mavis.memory.service import get_memory
from mavis.store.repo import loops as loop_repo
from mavis.store.repo import meeting_notes, notion_links, users
from mavis.store.repo import personal as personal_repo
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.native.base import NativeProvider
from mavis.tools.integrations.native.tokens import ACTIVE, Grant
from mavis.tools.integrations.normalize import extract_calendar_items, normalize_calendar_event, to_datetime
from mavis.tools.integrations.notion_sync import blocks, notes, rows
from mavis.tools.integrations.notion_sync.blocks import TEXT_LIMIT, content_hash
from mavis.tools.integrations.notion_sync.rows import is_suppressed
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


async def _prep(uid: int, m: Meeting, links: dict) -> dict:
    """What Prep says, as data: who is in the meeting and one item per graph fact or open loop, each tagged
    with the attendee emails it is about. Stored in the link instead of rendered blocks, so forgetting a
    person can take their lines out later (I5)."""
    people = [(p, links.get(p["email"])) for p in m.attendees]
    names = [_label(p, link) for p, link in people]
    items: list[dict] = []
    graph = get_memory().graph
    for (p, link), name in list(zip(people, names, strict=True))[:MAX_PEOPLE]:
        if link is None or not name:
            continue
        for fact in await graph.neighborhood(uid, [name], hops=1, limit=FACTS_PER_PERSON):
            items.append({"t": f"{name}: {fact}", "e": [p["email"]], "k": "fact"})
    by_name: dict[str, list[str]] = {}
    for (p, _), name in zip(people, names, strict=True):
        if name:
            by_name.setdefault(normalize_name(name), []).append(p["email"])
    open_loops = [lp for lp in await loop_repo.list_live(uid)
                  if set(by_name) & {normalize_name(e) for e in lp.entities}]
    for lp in open_loops[:MAX_LOOPS]:
        emails = sorted({e for n in {normalize_name(x) for x in lp.entities} for e in by_name.get(n, [])})
        items.append({"t": f"Open: {lp.title}", "e": emails})
    return {"with": [{"n": n, "e": p["email"]} for (p, _), n in zip(people, names, strict=True) if n],
            "items": items}


def _hit(emails: list[str], suppressed: set[str]) -> bool:
    return any(e and is_suppressed(e, suppressed) for e in emails)


def _drops(item: dict, suppressed: set[str], gone: set[str]) -> bool:
    """A suppressed person's lines all go; a gone person (no page any more) loses only the fact lines, the
    "With:" and "Open:" lines stay."""
    emails = item.get("e") or []
    return _hit(emails, suppressed) or (item.get("k") == "fact" and any(e in gone for e in emails))


def _prep_texts(prep: dict, suppressed: set[str], gone: set[str]) -> list[str]:
    who = [w["n"] for w in prep.get("with") or [] if not _hit([w.get("e", "")], suppressed)]
    return [*([f"With: {', '.join(who)}"] if who else []),
            *(i["t"] for i in prep.get("items") or [] if not _drops(i, suppressed, gone))]


def _prep_blocks(prep: dict, suppressed: set[str], gone: set[str]) -> list[dict]:
    return [blocks.heading("Prep"), *blocks.bullets(_prep_texts(prep, suppressed, gone))]


def _prep_hit(prep: dict, suppressed: set[str], gone: set[str]) -> bool:
    return any(_hit([w.get("e", "")], suppressed) for w in prep.get("with") or []) or any(
        _drops(i, suppressed, gone) for i in prep.get("items") or [])


async def _gone_emails(uid: int) -> set[str]:
    """Emails whose facts a frozen Prep holds but who no longer have a People page: forgotten without a
    suppression key (N1). A person is still there if any person link has the address as its key, one of its
    merged keys or its email, so a merge that switches the stored email loses nothing."""
    linked: set[str] = set()
    for p in await notion_links.all_of(uid, PERSON_LINK):
        linked |= {str(k).lower() for k in [p.local_key, p.meta.get("email"), *(p.meta.get("keys") or [])]
                   if k}
    out: set[str] = set()
    for link in await notion_links.all_of(uid, LINK):
        prep = link.meta.get("prep")
        for item in (prep.get("items") or []) if isinstance(prep, dict) else []:
            if item.get("k") == "fact":
                out |= {e for e in item.get("e") or [] if e and e.lower() not in linked}
    return out


async def _scrub(ctx: SyncCtx, suppressed: set[str], gone: set[str]) -> None:
    """Take a forgotten person's lines out of every Prep, in Notion and in the stored copy, including pages
    outside the sync window that are never rewritten otherwise (I5)."""
    if not suppressed and not gone:
        return
    uid = ctx.user_id
    for link in await notion_links.all_of(uid, LINK):
        prep = link.meta.get("prep")
        if not isinstance(prep, dict) or not _prep_hit(prep, suppressed, gone):
            continue
        if not ctx.left():
            return
        old_ids = list(link.meta.get("prep_ids") or [])
        kept = {"with": [w for w in prep.get("with") or [] if not _hit([w.get("e", "")], suppressed)],
                "items": [i for i in prep.get("items") or [] if not _drops(i, suppressed, gone)]}
        rest = [b for b in link.meta.get("blocks") or [] if b not in set(old_ids)]
        new_ids = await blocks.replace_owned(ctx.api, uid, link.page_id, old_ids,
                                             _prep_blocks(kept, suppressed, gone))
        await notion_links.upsert(uid, LINK, link.local_key, link.page_id, pushed_hash="",
                                  meta={**link.meta, "prep": kept, "prep_ids": new_ids,
                                        "blocks": [*new_ids, *rest]})


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


async def _sync_one(ctx: SyncCtx, m: Meeting, links: dict, suppressed: set[str],
                    gone: set[str]) -> None:
    uid = ctx.user_id
    link = await notion_links.get(uid, LINK, m.key)
    person_ids = sorted({links[p["email"]].page_id for p in m.attendees if p["email"] in links})
    props = _properties(m, person_ids)
    stored = link.meta.get("prep") if link is not None else None
    if isinstance(stored, dict) and m.start <= ctx.now:
        prep = stored  # started: Prep stays as it was (minus anyone forgotten since)
    else:
        prep = await _prep(uid, m, links)
    prep_blocks = _prep_blocks(prep, suppressed, gone)
    owned = [*prep_blocks, *_notes(m)]
    digest = content_hash({"props": props, "body": owned})
    if link is not None and link.pushed_hash == digest:
        return
    page_id = link.page_id if link is not None else ""
    fresh = link is None or bool(link.meta.get("fresh"))
    if link is not None and await rows.update_row(ctx.api, uid, ctx.home, "meetings", page_id, props) is None:
        await notion_links.delete(uid, LINK, m.key)  # the page is gone: make a new one
        link, fresh = None, True
    if link is None:
        page = await rows.create_row(ctx.api, uid, ctx.home, "meetings", LINK, m.key, props)
        page_id = page["id"]
        # link first: if the body write fails, the retry finishes this page instead of making another
        await notion_links.upsert(uid, LINK, m.key, page_id, pushed_hash="", meta={"fresh": True})
        await rows.created(uid, LINK, m.key)
    if fresh:
        ids = await blocks.init_page_body(ctx.api, uid, page_id, owned)
    else:
        ids = await blocks.replace_owned(ctx.api, uid, page_id, list(link.meta.get("blocks") or []), owned)
    await notion_links.upsert(uid, LINK, m.key, page_id, pushed_hash=digest, pushed_at=timeutil.now(),
                              meta={"blocks": ids, "prep": prep, "prep_ids": ids[:len(prep_blocks)],
                                    "title": m.title,
                                    "start": m.start.isoformat(),
                                    "emails": sorted(p["email"] for p in m.attendees if p["email"])})


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
    suppressed = await personal_repo.suppressed_keys(uid)
    gone = await _gone_emails(uid)
    await _scrub(ctx, suppressed, gone)
    for m in sorted(found.values(), key=lambda x: x.start):
        if not ctx.left():
            return
        await _sync_one(ctx, m, links, suppressed, gone)
    if ctx.left():
        await notes.read_your_notes(ctx, "meetings", LINK)  # R12 / I7
    if ctx.left():
        await ask_due(ctx, events)


register_pass("meetings", timedelta(minutes=60), meetings_pass, order=30)
