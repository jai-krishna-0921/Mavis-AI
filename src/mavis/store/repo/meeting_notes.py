# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Data access for meeting_notes: a meeting, its Fathom recording and what the user said about it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from mavis.domain import timeutil
from mavis.store.db import Session
from mavis.store.models import MeetingNote

_FIELDS = frozenset({"title", "starts_at", "ends_at", "attendees", "recording_id", "fathom_url", "summary",
                     "action_items", "user_notes", "asked_at"})
_TIMES = frozenset({"starts_at", "ends_at", "asked_at"})


@dataclass(frozen=True)
class MeetingNoteRow:
    id: int
    user_id: int
    event_key: str
    title: str
    starts_at: datetime | None
    ends_at: datetime | None
    attendees: list = field(default_factory=list)
    recording_id: str | None = None
    fathom_url: str = ""
    summary: str = ""
    action_items: list = field(default_factory=list)
    user_notes: str = ""
    asked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row(r: MeetingNote) -> MeetingNoteRow:
    return MeetingNoteRow(
        id=r.id, user_id=r.user_id, event_key=r.event_key, title=r.title or "",
        starts_at=timeutil.ensure_utc(r.starts_at), ends_at=timeutil.ensure_utc(r.ends_at),
        attendees=list(r.attendees or []), recording_id=r.recording_id, fathom_url=r.fathom_url or "",
        summary=r.summary or "", action_items=list(r.action_items or []), user_notes=r.user_notes or "",
        asked_at=timeutil.ensure_utc(r.asked_at), created_at=timeutil.ensure_utc(r.created_at),
        updated_at=timeutil.ensure_utc(r.updated_at),
    )


def _select(user_id: int, event_key: str):
    return select(MeetingNote).where(MeetingNote.user_id == user_id, MeetingNote.event_key == event_key)


def _new(user_id: int, event_key: str) -> MeetingNote:
    now = timeutil.now()
    return MeetingNote(user_id=user_id, event_key=event_key, title="", attendees=[], fathom_url="",
                       summary="", action_items=[], user_notes="", created_at=now, updated_at=now)


async def get(user_id: int, event_key: str) -> MeetingNoteRow | None:
    async with Session() as s:
        r = await s.scalar(_select(user_id, event_key))
        return _row(r) if r else None


async def by_recording(user_id: int, recording_id: str) -> MeetingNoteRow | None:
    async with Session() as s:
        r = await s.scalar(select(MeetingNote).where(
            MeetingNote.user_id == user_id, MeetingNote.recording_id == recording_id))
        return _row(r) if r else None


async def upsert(user_id: int, event_key: str, **fields: Any) -> MeetingNoteRow:
    """Create or update the note; only the fields given change."""
    unknown = set(fields) - _FIELDS
    if unknown:
        raise TypeError(f"unknown meeting note fields: {sorted(unknown)}")
    async with Session() as s:
        r = await s.scalar(_select(user_id, event_key))
        if r is None:
            r = _new(user_id, event_key)
            s.add(r)
        for name, value in fields.items():
            setattr(r, name, timeutil.ensure_utc(value) if name in _TIMES else value)
        r.updated_at = timeutil.now()
        await s.commit()
        await s.refresh(r)
        return _row(r)


async def append_user_notes(user_id: int, event_key: str, text: str) -> MeetingNoteRow:
    """Add `text` after what the user already said, separated by a blank line."""
    async with Session() as s:
        r = await s.scalar(_select(user_id, event_key))
        if r is None:
            r = _new(user_id, event_key)
            s.add(r)
        r.user_notes = f"{r.user_notes}\n\n{text}" if r.user_notes else text
        r.updated_at = timeutil.now()
        await s.commit()
        await s.refresh(r)
        return _row(r)


async def mark_asked(user_id: int, event_key: str, at: datetime) -> None:
    async with Session() as s:
        r = await s.scalar(_select(user_id, event_key))
        if r is not None:
            r.asked_at = timeutil.ensure_utc(at)
            r.updated_at = timeutil.now()
            await s.commit()


async def in_window(user_id: int, start: datetime, end: datetime) -> list[MeetingNoteRow]:
    """Meetings starting in [start, end), earliest first."""
    async with Session() as s:
        rows = await s.scalars(select(MeetingNote).where(
            MeetingNote.user_id == user_id, MeetingNote.starts_at >= start, MeetingNote.starts_at < end)
            .order_by(MeetingNote.starts_at, MeetingNote.id))
        return [_row(r) for r in rows]


async def clear_fathom(user_id: int) -> int:
    """Forget everything that came from Fathom. Rows the user wrote notes on stay; the rest are deleted.
    Returns how many rows were touched."""
    async with Session() as s:
        rows = list(await s.scalars(select(MeetingNote).where(MeetingNote.user_id == user_id)))
        touched = 0
        emptied: list[int] = []
        for r in rows:
            if not (r.recording_id or r.fathom_url or r.summary or r.action_items):
                continue  # calendar-only row: nothing from Fathom, keep it as it is (asked_at etc.)
            touched += 1
            if r.user_notes:
                r.recording_id, r.fathom_url, r.summary, r.action_items = None, "", "", []
                r.updated_at = timeutil.now()
            else:
                emptied.append(r.id)
        if emptied:
            await s.execute(delete(MeetingNote).where(MeetingNote.id.in_(emptied)))
        await s.commit()
        return touched
