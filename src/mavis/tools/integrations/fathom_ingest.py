# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Fathom recording ingestion: match a finished recording to a calendar event, keep it as the meeting's
note, turn its action items into loops and hand its summary to memory.

Everything Fathom sends is third-party text: it is coerced to bounded plain strings first, stored as data,
turned into UNTRUSTED loops and an UNTRUSTED memory record, and never written to a log (ids and statuses
only). Ingestion is idempotent per recording, so a webhook and the poll can both deliver the same one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from mavis.attention.connector_ingest import load_identities
from mavis.bus import get_bus
from mavis.domain.errors import FailureKind
from mavis.domain.events import Job, Trust
from mavis.domain.integrations import UserRef
from mavis.domain.loops import LoopKind, LoopOrigin, LoopUpsert
from mavis.loops.service import LoopService
from mavis.memory import controls, records
from mavis.store.repo import meeting_notes, users
from mavis.store.repo.loops import similar_titles
from mavis.tools.integrations import get_provider
from mavis.tools.integrations.native.base import NativeProvider, ReauthRequired
from mavis.tools.integrations.native.fathom_api import FathomApi, FathomError
from mavis.tools.integrations.native.providers import clean_text
from mavis.tools.integrations.native.tokens import ACTIVE
from mavis.tools.integrations.normalize import extract_calendar_items, normalize_calendar_event, to_datetime
from mavis.tools.integrations.notion_sync.runner import enqueue_sync, notion_grant

log = structlog.get_logger(__name__)

WINDOW = timedelta(hours=2)
NEAR = timedelta(minutes=15)
MIN_OVERLAP_S = 60.0
MIN_TRANSCRIPT_ROOM = 200
SUMMARY_CHARS = 20000
MAX_INVITEES = 50
MAX_ITEMS = 50
TRANSIENT = frozenset({FailureKind.UNAVAILABLE, FailureKind.RATE_LIMITED})


def fathom_api() -> FathomApi | None:
    """The router's own Fathom executor api, so ingestion shares its pacing bucket with the tools."""
    try:
        executor = get_provider().executors.get(NativeProvider.FATHOM)
    except AttributeError:
        return None
    return getattr(executor, "api", None)


async def calendar_events(user_id: int, start: datetime, end: datetime) -> list[dict]:
    res = await get_provider().execute(
        UserRef(user_id=user_id), "calendar.list",
        {"time_min": start.isoformat(), "time_max": end.isoformat(), "max_results": 50})
    return extract_calendar_items(res.data) if res.ok else []


# ----- coercion

def _email(value: object) -> str:
    text = str(value or "").strip().lower()
    return text[:200] if "@" in text else ""


def coerce(meeting: dict) -> dict | None:
    """Bounded plain values from a Fathom Meeting object, or None when it has no usable recording id."""
    rid = meeting.get("recording_id")
    if isinstance(rid, bool) or not isinstance(rid, int) or rid < 1:
        return None
    start = to_datetime(meeting.get("recording_start_time") or meeting.get("scheduled_start_time"))
    end = to_datetime(meeting.get("recording_end_time") or meeting.get("scheduled_end_time")) or start
    invitees = []
    for inv in (meeting.get("calendar_invitees") or [])[:MAX_INVITEES]:
        if not isinstance(inv, dict):
            continue
        entry = {"name": clean_text(inv.get("name"), 80), "email": _email(inv.get("email"))}
        if entry["name"] or entry["email"]:
            invitees.append(entry)
    items = []
    for item in (meeting.get("action_items") or [])[:MAX_ITEMS]:
        if not isinstance(item, dict) or not (text := clean_text(item.get("description"), 500)):
            continue
        who = item.get("assignee") if isinstance(item.get("assignee"), dict) else {}
        items.append({"description": text, "assignee_name": clean_text(who.get("name"), 80),
                      "assignee_email": _email(who.get("email")), "completed": item.get("completed") is True})
    summary = meeting.get("default_summary")
    summary = summary.get("markdown_formatted") if isinstance(summary, dict) else None
    return {
        "recording_id": rid,
        "title": clean_text(meeting.get("title") or meeting.get("meeting_title"), 300),
        "start": start, "end": end, "invitees": invitees, "action_items": items,
        "url": str(meeting.get("share_url") or meeting.get("url") or "")[:500],
        "summary": summary.strip()[:SUMMARY_CHARS] if isinstance(summary, str) else "",
    }


# ----- matching

def _span(raw: dict) -> tuple[datetime, datetime] | None:
    ev = normalize_calendar_event(raw)
    start, end = to_datetime(ev["start"]), to_datetime(ev["end"])
    if start is None:
        return None
    return start, max(end or start, start)


def match_event(meeting: dict, events: list[dict]) -> dict | None:
    """The calendar event this recording belongs to. A candidate overlaps the recording by a minute or is
    at most 15 minutes away from it; the best has the most overlap, then the most shared invitee
    addresses, then a similar title."""
    start = meeting.get("start")
    if start is None:
        return None
    end = meeting.get("end") or start
    emails = {i["email"] for i in meeting.get("invitees") or [] if i.get("email")}
    best, best_rank = None, None
    for raw in events:
        span = _span(raw)
        norm = normalize_calendar_event(raw)
        if span is None or norm["status"] == "cancelled":
            continue
        overlap = max(0.0, (min(end, span[1]) - max(start, span[0])).total_seconds())
        gap = max(timedelta(0), span[0] - end, start - span[1])
        if overlap < MIN_OVERLAP_S and gap > NEAR:
            continue
        shared = len(emails & {a.lower() for a in norm["attendees"]})
        rank = (overlap, shared, similar_titles(str(meeting.get("title") or ""), norm["summary"]))
        if best_rank is None or rank > best_rank:
            best, best_rank = raw, rank
    return best


def _event_id(event: dict) -> str:
    return normalize_calendar_event(event)["event_id"]


# ----- ingestion

async def _fetch(call, default: str = "") -> str:
    """One Fathom read. A transient failure propagates so the job retries; anything else leaves the
    default (the meeting note is still kept)."""
    try:
        return await call
    except FathomError as exc:
        if exc.kind in TRANSIENT:
            raise
        log.info("fathom.fetch_failed", kind=exc.kind.value, code=exc.code)
    except ReauthRequired:
        log.info("fathom.fetch_reauth")
    return default


async def _event_key(user_id: int, m: dict) -> str:
    fallback = f"fathom:{m['recording_id']}"
    known = await meeting_notes.by_recording(user_id, str(m["recording_id"]))
    if known is not None:
        return known.event_key
    if m["start"] is None:
        return fallback
    end = m["end"] or m["start"]
    event = match_event(m, await calendar_events(user_id, m["start"] - WINDOW, end + WINDOW))
    key = _event_id(event) if event is not None else ""
    if not key:
        return fallback
    row = await meeting_notes.get(user_id, key)
    if row is not None and row.recording_id and row.recording_id != str(m["recording_id"]):
        return fallback  # that event already has another recording
    return key


async def _loops(user_id: int, m: dict, self_emails: set[str]) -> None:
    invitee_emails = {i["email"] for i in m["invitees"] if i["email"]}
    ref = f"fathom:{m['recording_id']}"
    service = LoopService(get_bus())
    for item in m["action_items"]:
        email = item["assignee_email"]
        if item["completed"] or not email:
            continue
        if email in self_emails:
            kind, entities = LoopKind.COMMITMENT, []
        elif email in invitee_emails:
            kind, entities = LoopKind.WAITING_ON, [item["assignee_name"] or email]
        else:
            continue
        await service.upsert(
            user_id,
            LoopUpsert(kind=kind, title=item["description"], entities=entities, trust=Trust.UNTRUSTED,
                       origin=LoopOrigin.MEETING, source=ref),
            anchor_at=m["end"] or m["start"])


async def _memory(user_id: int, api: FathomApi | None, m: dict, summary: str, identities: dict) -> None:
    if "fathom" in await controls.paused(user_id):
        return
    user = await users.get(user_id)

    def record(text: str):
        return records.meeting_record(user_id, m["recording_id"], m["title"], m["start"], m["invitees"], text,
                                      self_ids=identities, self_names=[user.name or ""])

    job = record(summary)
    if job is None:
        return
    # the record keeps RECORD_TEXT_CAP characters in all: only ask for the transcript the room left holds
    room = records.RECORD_TEXT_CAP - len(job.text) - len("\n\n")
    if api is not None and room >= MIN_TRANSCRIPT_ROOM:
        transcript = await _fetch(api.transcript(user_id, m["recording_id"], room))
        if transcript.strip():
            job = record(f"{summary}\n\n{transcript[:room]}".strip()) or job
    if job.text.partition("\n\n")[2].strip():
        await records.submit(job)


async def ingest_meeting(user_id: int, meeting: dict, *, source: str) -> str | None:
    """Store one recording. Returns the meeting's event key (a calendar event id, else `fathom:<id>`), or
    None when the payload has no usable recording id. A recording already ingested (the webhook and the poll
    both deliver, Fathom re-delivers) makes no Fathom call and no memory record; its note, loops and Notion
    sync are refreshed from the payload."""
    m = coerce(meeting)
    if m is None:
        return None
    known = await meeting_notes.by_recording(user_id, str(m["recording_id"]))
    api = None if known is not None else fathom_api()
    summary = m["summary"] or (known.summary if known is not None else "")
    if not summary and api is not None:
        summary = (await _fetch(api.summary(user_id, m["recording_id"])))[:SUMMARY_CHARS].strip()
    key = known.event_key if known is not None else await _event_key(user_id, m)
    identities = await load_identities(user_id)
    if known is None:  # the note is written last, so a retry after a failure here starts over
        await _memory(user_id, api, m, summary, identities)
    await _loops(user_id, m, set(identities["emails"]))
    await meeting_notes.upsert(
        user_id, key, recording_id=str(m["recording_id"]), fathom_url=m["url"], summary=summary,
        action_items=m["action_items"], title=m["title"], starts_at=m["start"], ends_at=m["end"],
        attendees=m["invitees"])
    grant = await notion_grant(user_id)
    if grant is not None and grant.status == ACTIVE:
        await enqueue_sync(user_id, force={"meetings"})
    log.info("fathom.ingested", user_id=user_id, recording_id=m["recording_id"], source=source,
             repeat=known is not None)
    return key


async def fathom_recording_job(job: Job) -> None:
    meeting = job.payload.get("meeting")
    if not isinstance(meeting, dict):
        return
    try:
        await ingest_meeting(job.user_id, meeting, source=str(job.payload.get("source") or ""))
    except ReauthRequired:
        log.info("fathom.ingest_reauth", user_id=job.user_id)  # the connection check owns this
