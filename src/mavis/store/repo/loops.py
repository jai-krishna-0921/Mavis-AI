"""Data access for open loops."""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from mavis.domain import timeutil
from mavis.domain.loops import Loop, LoopKind, LoopStatus, LoopUpsert, WatchSpec
from mavis.store.db import Session
from mavis.store.models import LoopRow

STALE_AFTER = timedelta(days=2)
AWAITING_FOR = timedelta(hours=24)  # a follow-up nobody answered closes its loop after this
EXPIRING_KINDS = (LoopKind.COMMITMENT.value, LoopKind.WAITING_ON.value, LoopKind.WATCH.value)


def to_domain(r: LoopRow) -> Loop:
    return Loop(
        id=r.id,
        user_id=r.user_id,
        kind=LoopKind(r.kind),
        title=r.title,
        due_at=timeutil.ensure_utc(r.due_at),
        entities=list(r.entities or []),
        status=LoopStatus(r.status),
        importance=r.importance,
        watch=WatchSpec.model_validate(r.watch) if r.watch else None,
        source=r.source,
        version=r.version or 1,
    )


def _watch_json(data: LoopUpsert) -> dict | None:
    return data.watch.model_dump(mode="json") if data.watch else None


async def insert(user_id: int, data: LoopUpsert) -> Loop:
    now = timeutil.now()
    async with Session() as s:
        row = LoopRow(
            user_id=user_id,
            kind=data.kind.value,
            title=data.title,
            due_at=timeutil.ensure_utc(data.due_at),
            entities=list(data.entities),
            status=data.status.value,
            importance=data.importance,
            watch=_watch_json(data),
            source=data.source,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return to_domain(row)


async def update(user_id: int, loop_id: int, data: LoopUpsert) -> tuple[Loop, bool] | None:
    """Merge `data` into the loop; returns (loop, changed). Version/updated_at move only on change."""
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        if row is None or row.user_id != user_id:
            return None
        before = to_domain(row)
        row.kind, row.title, row.status, row.importance = (
            data.kind.value,
            data.title,
            data.status.value,
            data.importance,
        )
        if data.due_at is not None:
            row.due_at = timeutil.ensure_utc(data.due_at)
        merged = list(row.entities or [])
        merged += [e for e in data.entities if e.casefold() not in {m.casefold() for m in merged}]
        row.entities = merged
        if data.watch is not None:
            row.watch = _watch_json(data)
        if data.source:
            row.source = data.source
        changed = to_domain(row) != before
        if changed:
            row.updated_at = timeutil.now()
            row.version = (row.version or 1) + 1
            await s.commit()
            await s.refresh(row)
        return to_domain(row), changed


async def get(loop_id: int) -> Loop | None:
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        return to_domain(row) if row else None


async def list_open(user_id: int) -> list[Loop]:
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status == LoopStatus.OPEN.value)
        )
        return [to_domain(r) for r in rows]


DUPLICATE_DUE_WINDOW = timedelta(hours=2)
DUPLICATE_SIMILARITY = 0.8
_TIME_TOKEN = re.compile(r"^\d+(?:[:.]\d+)?(?:am|pm|h|hrs?|st|nd|rd|th)?$")
_DUP_STOPWORDS = frozenset(
    "a an the at on by in to for of with and or my me i is it this that next coming about from "
    "am pm today tomorrow tonight tmrw tmr morning afternoon evening night noon midnight "
    "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun "
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)


def title_tokens(title: str) -> list[str]:
    """Title words that identify the thing: no times, dates, weekdays or filler."""
    return [t for t in normalise_title(title).split() if t not in _DUP_STOPWORDS and not _TIME_TOKEN.match(t)]


def similar_titles(a: str, b: str) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return normalise_title(a) == normalise_title(b)
    sa, sb = set(ta), set(tb)
    if len(sa & sb) / len(sa | sb) >= DUPLICATE_SIMILARITY:
        return True
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(sa)), " ".join(sorted(sb))).ratio()
    return ratio >= DUPLICATE_SIMILARITY


def _due_close(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True
    return abs(timeutil.ensure_utc(a) - timeutil.ensure_utc(b)) <= DUPLICATE_DUE_WINDOW


async def find_open_duplicate(user_id: int, data: LoopUpsert) -> Loop | None:
    """An open loop of the same kind that is the same thing said differently (one utterance often
    yields "Dentist appointment" and "Dentist appointment at 4pm"): similar title and due within 2h,
    or either due missing. An exact match wins over a fuzzy one."""
    due = timeutil.ensure_utc(data.due_at)
    fuzzy: Loop | None = None
    for loop in await list_open(user_id):
        if loop.kind is not data.kind:
            continue
        if loop.title.casefold() == data.title.casefold() and loop.due_at == due:
            return loop
        if fuzzy is None and _due_close(loop.due_at, due) and similar_titles(loop.title, data.title):
            fuzzy = loop
    return fuzzy


def normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


async def find_recently_closed(user_id: int, title: str, since: datetime) -> Loop | None:
    """A DONE/DROPPED (or awaiting-reply) loop with the same normalised title closed after `since`."""
    wanted = normalise_title(title)
    closed = (LoopStatus.DONE.value, LoopStatus.DROPPED.value, LoopStatus.AWAITING_REPLY.value)
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(
                LoopRow.user_id == user_id, LoopRow.status.in_(closed), LoopRow.updated_at >= since
            )
        )
        for r in rows:
            if normalise_title(r.title) == wanted:
                return to_domain(r)
    return None


async def set_status(user_id: int, loop_id: int, status: LoopStatus) -> tuple[Loop, bool] | None:
    async with Session() as s:
        row = await s.get(LoopRow, loop_id)
        if row is None or row.user_id != user_id:
            return None
        if row.status == status.value:
            return to_domain(row), False
        row.status = status.value
        row.updated_at = timeutil.now()
        row.version = (row.version or 1) + 1
        await s.commit()
        await s.refresh(row)
        return to_domain(row), True


_EXPIRY_STATUSES = (LoopStatus.OPEN.value, LoopStatus.AWAITING_REPLY.value)


async def open_user_ids() -> list[int]:
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow.user_id).where(LoopRow.status.in_(_EXPIRY_STATUSES)).distinct()
        )
        return list(rows)


async def list_awaiting(user_id: int, since: datetime) -> list[tuple[Loop, datetime]]:
    """Loops waiting on the user's reply to a follow-up sent after `since`, with when they started waiting."""
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id,
                                  LoopRow.status == LoopStatus.AWAITING_REPLY.value,
                                  LoopRow.updated_at >= since)
        )
        return [(to_domain(r), timeutil.ensure_utc(r.updated_at)) for r in rows]


async def expire(user_id: int, now) -> list[Loop]:
    """Mark one user's stale OPEN loops EXPIRED, and close loops whose follow-up got no reply within
    AWAITING_FOR; returns the loops that changed."""
    expired: list[Loop] = []
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status.in_(_EXPIRY_STATUSES))
        )
        for row in rows:
            if row.status == LoopStatus.AWAITING_REPLY.value:
                if timeutil.ensure_utc(row.updated_at) < now - AWAITING_FOR:
                    row.status = LoopStatus.DONE.value
                    row.updated_at = now
                    row.version = (row.version or 1) + 1
                    expired.append(to_domain(row))
                continue
            due = timeutil.ensure_utc(row.due_at)
            deadline = (
                timeutil.ensure_utc(WatchSpec.model_validate(row.watch).deadline) if row.watch else None
            )
            stale_due = row.kind in EXPIRING_KINDS and due is not None and due < now - STALE_AFTER
            stale_watch = deadline is not None and deadline < now
            if stale_due or stale_watch:
                row.status = LoopStatus.EXPIRED.value
                row.updated_at = now
                row.version = (row.version or 1) + 1
                expired.append(to_domain(row))
        await s.commit()
    return expired
