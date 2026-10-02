"""Data access for wakeups. claim_due / fire_due are safe under concurrent timers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

import structlog
from sqlalchemy import select, update

from mavis.domain import timeutil
from mavis.domain.wakeups import Wakeup, WakeupKind, WakeupStatus
from mavis.store.db import Session
from mavis.store.models import WakeupRow

log = structlog.get_logger()
PENDING = WakeupStatus.PENDING.value


def to_domain(r: WakeupRow, status: WakeupStatus | None = None) -> Wakeup:
    return Wakeup(
        id=r.id, user_id=r.user_id, due_at=timeutil.ensure_utc(r.due_at), kind=WakeupKind(r.kind),
        reason=r.reason, loop_id=r.loop_id, payload=dict(r.payload or {}),
        status=status or WakeupStatus(r.status),
    )


async def insert(*, user_id: int, due_at: datetime, kind: WakeupKind, reason: str, loop_id: int | None,
                 payload: dict, dedupe_key: str | None) -> int:
    async with Session() as s:
        row = WakeupRow(user_id=user_id, due_at=due_at, kind=kind.value, reason=reason, loop_id=loop_id,
                        payload=payload, status=PENDING, dedupe_key=dedupe_key, created_at=timeutil.now())
        s.add(row)
        await s.commit()
        return row.id


async def pending_by_key(user_id: int, dedupe_key: str) -> Wakeup | None:
    async with Session() as s:
        row = await s.scalar(select(WakeupRow).where(
            WakeupRow.user_id == user_id, WakeupRow.dedupe_key == dedupe_key, WakeupRow.status == PENDING))
        return to_domain(row) if row else None


async def list_pending(user_id: int, kind: WakeupKind | None = None) -> list[Wakeup]:
    q = select(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == PENDING)
    if kind is not None:
        q = q.where(WakeupRow.kind == kind.value)
    async with Session() as s:
        return [to_domain(r) for r in await s.scalars(q.order_by(WakeupRow.due_at, WakeupRow.id))]


async def cancel_ids(ids: Iterable[int]) -> int:
    ids = list(ids)
    if not ids:
        return 0
    async with Session() as s:
        res = await s.execute(update(WakeupRow).where(WakeupRow.id.in_(ids), WakeupRow.status == PENDING)
                              .values(status=WakeupStatus.CANCELLED.value))
        await s.commit()
        return res.rowcount or 0


async def cancel_where(user_id: int, kinds: Iterable[WakeupKind], loop_id: int | None) -> int:
    q = update(WakeupRow).where(WakeupRow.user_id == user_id, WakeupRow.status == PENDING,
                                WakeupRow.kind.in_([k.value for k in kinds]))
    if loop_id is not None:
        q = q.where(WakeupRow.loop_id == loop_id)
    async with Session() as s:
        res = await s.execute(q.values(status=WakeupStatus.CANCELLED.value))
        await s.commit()
        return res.rowcount or 0


async def reschedule(wakeup_id: int, at: datetime) -> bool:
    async with Session() as s:
        res = await s.execute(update(WakeupRow).where(WakeupRow.id == wakeup_id, WakeupRow.status == PENDING)
                              .values(due_at=at))
        await s.commit()
        return (res.rowcount or 0) == 1


def _due_query(now: datetime, limit: int):
    return (select(WakeupRow).where(WakeupRow.status == PENDING, WakeupRow.due_at <= now)
            .order_by(WakeupRow.due_at, WakeupRow.id).limit(limit))


async def fire_due(now: datetime, limit: int,
                   publish: Callable[[Wakeup], Awaitable[None]] | None = None) -> list[Wakeup]:
    """Move due PENDING rows to FIRED, calling `publish` for each BEFORE it is marked fired.

    If `publish` raises, rows already published stay FIRED, the failing row (and the rest) stay PENDING so
    the next tick retries, and the error propagates. Publishing is deduped by event id downstream, so a
    crash between publish and mark re-publishes at most once more, harmlessly.

    Postgres: SELECT ... FOR UPDATE SKIP LOCKED in one transaction, so concurrent timers never see the
    same row. SQLite (dev/tests): compare-and-set per row on status.
    """
    q = _due_query(now, limit)
    fired: list[Wakeup] = []
    async with Session() as s:
        postgres = s.get_bind().dialect.name == "postgresql"
        if postgres:
            q = q.with_for_update(skip_locked=True)
        try:
            for row in list(await s.scalars(q)):
                w = to_domain(row, WakeupStatus.FIRED)
                if publish is not None:
                    await publish(w)
                if postgres:
                    row.status, row.fired_at = WakeupStatus.FIRED.value, now
                else:
                    res = await s.execute(update(WakeupRow).where(WakeupRow.id == row.id,
                                                                  WakeupRow.status == PENDING)
                                          .values(status=WakeupStatus.FIRED.value, fired_at=now))
                    if res.rowcount != 1:
                        continue  # another timer fired it between our select and update
                fired.append(w)
        except BaseException:
            try:
                await s.commit()  # keep what was already published; never mask the original error
            except Exception:  # noqa: BLE001
                log.warning("wakeups.commit_after_error_failed", exc_info=True)
            raise
        await s.commit()
    return fired


async def claim_due(now: datetime, limit: int) -> list[Wakeup]:
    """Atomically move due PENDING rows to FIRED and return them (no publishing)."""
    return await fire_due(now, limit)
