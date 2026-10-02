"""Data access for open loops."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select

from mavis.domain import timeutil
from mavis.domain.loops import Loop, LoopKind, LoopStatus, LoopUpsert, WatchSpec
from mavis.store.db import Session
from mavis.store.models import LoopRow

STALE_AFTER = timedelta(days=2)
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


async def find_open_duplicate(user_id: int, data: LoopUpsert) -> Loop | None:
    due = timeutil.ensure_utc(data.due_at)
    for loop in await list_open(user_id):
        if loop.kind is data.kind and loop.title.casefold() == data.title.casefold() and loop.due_at == due:
            return loop
    return None


def normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


async def find_recently_closed(user_id: int, title: str, since: datetime) -> Loop | None:
    """A DONE/DROPPED loop with the same normalised title closed after `since`."""
    wanted = normalise_title(title)
    closed = (LoopStatus.DONE.value, LoopStatus.DROPPED.value)
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


async def open_user_ids() -> list[int]:
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow.user_id).where(LoopRow.status == LoopStatus.OPEN.value).distinct()
        )
        return list(rows)


async def expire(user_id: int, now) -> list[Loop]:
    """Mark one user's stale OPEN loops EXPIRED; returns the loops that changed."""
    expired: list[Loop] = []
    async with Session() as s:
        rows = await s.scalars(
            select(LoopRow).where(LoopRow.user_id == user_id, LoopRow.status == LoopStatus.OPEN.value)
        )
        for row in rows:
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
